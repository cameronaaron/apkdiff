//! Compare two dex inventories and emit exactly the artefacts stage 3 needs.
//!
//! Output shapes are deliberately identical to the ones the apktool/smali path
//! produced, so `apkdiff-findings.py` and the reports keep working unchanged.

use crate::dex::{Class, Member};
use std::collections::{BTreeMap, BTreeSet};

pub type Inventory = BTreeMap<String, Class>;

#[derive(Debug, PartialEq, Eq)]
pub enum Status {
    New,
    Gone,
    Changed,
}

impl Status {
    pub fn label(&self) -> &'static str {
        match self {
            Status::New => "NEW",
            Status::Gone => "GONE",
            Status::Changed => "CHANGED",
        }
    }
}

#[derive(Debug)]
pub struct ClassDelta {
    pub name: String,
    pub status: Status,
    pub added: Vec<String>,
    pub removed: Vec<String>,
    /// Members whose signature is unchanged but whose bytecode differs. Named
    /// rather than counted: "CallActivity.onCreate was rewired" is a
    /// lead, "1 method body changed" is not.
    pub changed: Vec<String>,
}

impl ClassDelta {
    /// How much of this class moved. Used to rank "where to start reading":
    /// a signature change and a body change both count for one.
    pub fn magnitude(&self) -> usize {
        self.added.len() + self.removed.len() + self.changed.len()
    }
}

/// Dalvik descriptors (`Lcom/example/Thing;`) rendered as smali-style paths
/// (`com/example/Thing`), which is how the rest of the tool names classes.
pub fn descriptor_to_path(d: &str) -> String {
    let s = d.strip_prefix('L').unwrap_or(d);
    s.strip_suffix(';').unwrap_or(s).to_string()
}

fn by_line(c: &Class) -> BTreeMap<&str, &Member> {
    c.members.iter().map(|m| (m.line.as_str(), m)).collect()
}

/// Merge several dex files into one inventory and one literal pool.
///
/// R8 decides which class lands in which dex, and that assignment shifts
/// between builds for reasons that have nothing to do with the source. Compared
/// dex-by-dex, a class that merely moved from classes.dex to classes3.dex reads
/// as removed from one and added to the other, and every string it carries
/// looks new in the receiving file. Pooling first makes the comparison depend
/// on what the build contains rather than on how R8 sharded it.
///
/// Pooling the *changed* dex files is sufficient: a dex that gained or lost a
/// class necessarily changed, so a class cannot move into or out of a file that
/// compared byte-identical.
pub fn pool(dexes: &[crate::dex::Dex]) -> (Inventory, Vec<String>) {
    // Reading a dex is the most expensive thing the tool does — every method
    // body of every class walked instruction by instruction — and the shards
    // are independent until they are merged. Each gets a thread; the results
    // are merged in argument order afterwards, so the pool is byte-identical
    // to the sequential one however the threads finish.
    let parts: Vec<(Inventory, Vec<String>)> = std::thread::scope(|scope| {
        let workers: Vec<_> = dexes
            .iter()
            .map(|d| scope.spawn(|| (d.inventory(), d.literals())))
            .collect();
        workers
            .into_iter()
            .map(|w| w.join().expect("a dex reader thread panicked"))
            .collect()
    });

    let mut inv: Inventory = BTreeMap::new();
    let mut lits: BTreeSet<String> = BTreeSet::new();
    for (one, one_lits) in parts {
        for (name, class) in one {
            match inv.entry(name) {
                std::collections::btree_map::Entry::Vacant(e) => {
                    e.insert(class);
                }
                // One class in two shards is not something R8 emits, but
                // merging rather than overwriting keeps the pool honest if it
                // ever happens: a dropped half would read as removed members.
                std::collections::btree_map::Entry::Occupied(mut e) => {
                    let slot = e.get_mut();
                    slot.members.extend(class.members);
                    slot.members.sort();
                    slot.members.dedup();
                    slot.literals.extend(class.literals);
                }
            }
        }
        lits.extend(one_lits);
    }
    (inv, lits.into_iter().collect())
}

pub fn compare(old: &Inventory, new: &Inventory) -> Vec<ClassDelta> {
    let mut out = Vec::new();
    // Walking the two maps separately rather than the union of their keys
    // means every branch here is reachable: a union walk needs an arm for the
    // name that is in neither map, which cannot happen and so can never be
    // tested.
    for (name, n) in new {
        let path = descriptor_to_path(name);
        match old.get(name) {
            Some(o) => {
                let om = by_line(o);
                let nm = by_line(n);
                let added: Vec<String> = nm
                    .keys()
                    .filter(|k| !om.contains_key(*k))
                    .map(|k| k.to_string())
                    .collect();
                let removed: Vec<String> = om
                    .keys()
                    .filter(|k| !nm.contains_key(*k))
                    .map(|k| k.to_string())
                    .collect();
                // `None` means the body could not be decoded, on either
                // side — an abstract or native method, or an instruction walk
                // that did not land exactly on the end of the array. Only two
                // hashes that both exist can disagree; comparing against a
                // `None` would turn a decode failure into a confident claim
                // that the method was rewritten.
                let changed: Vec<String> = om
                    .iter()
                    .filter(
                        |(k, ov)| match (ov.body, nm.get(*k).and_then(|nv| nv.body)) {
                            (Some(a), Some(b)) => a != b,
                            _ => false,
                        },
                    )
                    .map(|(k, _)| k.to_string())
                    .collect();
                if !added.is_empty() || !removed.is_empty() || !changed.is_empty() {
                    out.push(ClassDelta {
                        name: path,
                        status: Status::Changed,
                        added,
                        removed,
                        changed,
                    });
                }
            }
            // A whole new class arrives with all of its members: they are as
            // new as the class is, and dropping them left every method on a
            // newly added class invisible to the triage stage.
            None => out.push(ClassDelta {
                name: path,
                status: Status::New,
                added: n.members.iter().map(|m| m.line.clone()).collect(),
                removed: Vec::new(),
                changed: Vec::new(),
            }),
        }
    }
    for (name, o) in old {
        if !new.contains_key(name) {
            out.push(ClassDelta {
                name: descriptor_to_path(name),
                status: Status::Gone,
                added: Vec::new(),
                removed: o.members.iter().map(|m| m.line.clone()).collect(),
                changed: Vec::new(),
            });
        }
    }
    // Two passes produce two runs; sort so the artefacts are ordered by class
    // regardless of which side a class came from.
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

/// `STATUS<TAB>class`, sorted — consumed by the reports and by findings.
pub fn norm_lines(deltas: &[ClassDelta]) -> Vec<String> {
    let mut v: Vec<String> = deltas
        .iter()
        .map(|d| format!("{}\t{}", d.status.label(), d.name))
        .collect();
    v.sort();
    v
}

/// `class<TAB>+|-|~<TAB>signature` — the machine-readable member delta.
/// `~` marks a member whose signature held but whose body was rewritten.
pub fn api_lines(deltas: &[ClassDelta]) -> Vec<String> {
    let mut v = Vec::new();
    for d in deltas {
        for s in &d.removed {
            v.push(format!("{}\t-\t{}", d.name, s));
        }
        for s in &d.added {
            v.push(format!("{}\t+\t{}", d.name, s));
        }
        for s in &d.changed {
            v.push(format!("{}\t~\t{}", d.name, s));
        }
    }
    v
}

/// `literal<TAB>class` for every literal the new build gained, naming the
/// classes whose code loads it.
///
/// A bare list of new strings says what the build now says; this says who says
/// it, which is the difference between "a dialog string appeared" and "the
/// call screen gained a dialog".
pub fn owner_lines(old: &Inventory, new: &Inventory) -> Vec<String> {
    let before: BTreeSet<&String> = old.values().flat_map(|c| c.literals.iter()).collect();
    let mut v = Vec::new();
    for (name, c) in new {
        for lit in &c.literals {
            if !before.contains(lit) {
                v.push(format!("{}\t{}", lit, descriptor_to_path(name)));
            }
        }
    }
    v.sort();
    v
}

/// `magnitude<TAB>class`, biggest first: the "start reading here" ranking.
pub fn rank_lines(deltas: &[ClassDelta]) -> Vec<String> {
    let mut ranked: Vec<&ClassDelta> = deltas
        .iter()
        .filter(|d| d.status == Status::Changed)
        .collect();
    ranked.sort_by(|a, b| b.magnitude().cmp(&a.magnitude()).then(a.name.cmp(&b.name)));
    ranked
        .iter()
        .map(|d| format!("{:6}  {}", d.magnitude(), d.name))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dex::Dex;
    use crate::testdex;

    fn member(line: &str, body: Option<u64>) -> Member {
        Member {
            line: line.to_string(),
            body,
        }
    }

    /// A class name and its members, each a declaration line and body hash.
    type ClassSpec<'a> = (&'a str, &'a [(&'a str, Option<u64>)]);

    fn inv(pairs: &[ClassSpec]) -> Inventory {
        pairs
            .iter()
            .map(|(name, members)| {
                (
                    name.to_string(),
                    Class {
                        members: members.iter().map(|(l, b)| member(l, *b)).collect(),
                        literals: BTreeSet::new(),
                    },
                )
            })
            .collect()
    }

    #[test]
    fn a_class_present_only_in_the_old_build_is_removed_with_its_members() {
        let a = inv(&[("La;", &[(".method gone()V", Some(1))])]);
        let d = compare(&a, &Inventory::new());
        assert_eq!(d[0].status, Status::Gone);
        assert_eq!(d[0].removed, vec![".method gone()V"]);
        assert_eq!(norm_lines(&d), vec!["GONE\ta"]);
    }

    #[test]
    fn every_shard_survives_the_parallel_read() {
        use crate::dex::Dex;
        // The shards are read on separate threads and their results collected
        // afterwards, so the failure this guards against is a shard whose
        // result never reaches the merge. Folding each shard's own pool is the
        // sequential definition; the parallel read must land on exactly that,
        // every class, member and literal included. It cannot catch a merge
        // performed in the wrong order, because merging into a keyed map with
        // sorted, deduplicated members gives the same answer in any order —
        // what it catches is content that went missing on the way.
        let dexes = [
            Dex::parse(crate::testdex::sample()).unwrap(),
            Dex::parse(crate::testdex::sample_variant()).unwrap(),
            Dex::parse(crate::testdex::sample_jumbo()).unwrap(),
        ];
        let (inv, lits) = pool(&dexes);

        let mut want = Inventory::new();
        let mut want_lits: BTreeSet<String> = BTreeSet::new();
        for d in &dexes {
            let (one, one_lits) = pool(std::slice::from_ref(d));
            for (name, class) in one {
                let slot = want.entry(name).or_insert(Class {
                    members: Vec::new(),
                    literals: BTreeSet::new(),
                });
                slot.members.extend(class.members);
                slot.members.sort();
                slot.members.dedup();
                slot.literals.extend(class.literals);
            }
            want_lits.extend(one_lits);
        }
        assert_eq!(
            inv.keys().collect::<Vec<_>>(),
            want.keys().collect::<Vec<_>>()
        );
        for (name, class) in &inv {
            assert_eq!(class.members, want[name].members, "{name} members");
            assert_eq!(class.literals, want[name].literals, "{name} literals");
        }
        assert_eq!(lits, want_lits.into_iter().collect::<Vec<_>>());
    }

    #[test]
    fn pooling_merges_a_class_that_somehow_appears_in_two_shards() {
        use crate::dex::Dex;
        // Two copies of the same fixture: R8 does not emit this, but a
        // pooled read must not drop half of a class if it ever does.
        let dexes = [
            Dex::parse(crate::testdex::sample()).unwrap(),
            Dex::parse(crate::testdex::sample_jumbo()).unwrap(),
        ];
        let (inv, lits) = pool(&dexes);
        let thing = &inv["Lcom/example/Thing;"];
        assert!(thing.literals.contains("\"hello world\""));
        assert!(
            thing.members.iter().any(|m| m.line.contains("run()V")),
            "members from both shards survive"
        );
        assert!(lits.contains(&"\"hello world\"".to_string()));
    }

    #[test]
    fn descriptor_conversion() {
        assert_eq!(
            descriptor_to_path("Lcom/example/Thing;"),
            "com/example/Thing"
        );
        assert_eq!(descriptor_to_path("I"), "I");
        assert_eq!(descriptor_to_path("Lnosemi"), "nosemi");
    }

    #[test]
    fn status_labels() {
        assert_eq!(Status::New.label(), "NEW");
        assert_eq!(Status::Gone.label(), "GONE");
        assert_eq!(Status::Changed.label(), "CHANGED");
        assert!(format!("{:?}", Status::New).contains("New"));
    }

    #[test]
    fn detects_new_and_gone_classes() {
        let a = inv(&[("La;", &[])]);
        let b = inv(&[("Lb;", &[])]);
        let d = compare(&a, &b);
        assert_eq!(d.len(), 2);
        assert_eq!(norm_lines(&d), vec!["GONE\ta", "NEW\tb"]);
    }

    #[test]
    fn identical_inventories_produce_nothing() {
        let a = inv(&[("La;", &[(".method x()V", Some(1))])]);
        assert!(compare(&a, &a).is_empty());
    }

    #[test]
    fn detects_added_and_removed_members() {
        let a = inv(&[("La;", &[(".method old()V", Some(1))])]);
        let b = inv(&[("La;", &[(".method new()V", Some(1))])]);
        let d = compare(&a, &b);
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].added, vec![".method new()V"]);
        assert_eq!(d[0].removed, vec![".method old()V"]);
        assert!(d[0].changed.is_empty());
        assert_eq!(d[0].magnitude(), 2);
        assert_eq!(
            api_lines(&d),
            vec!["a\t-\t.method old()V", "a\t+\t.method new()V"]
        );
    }

    #[test]
    fn detects_a_changed_body_behind_an_unchanged_signature() {
        let a = inv(&[("La;", &[(".method x()V", Some(1))])]);
        let b = inv(&[("La;", &[(".method x()V", Some(2))])]);
        let d = compare(&a, &b);
        assert_eq!(d[0].changed, vec![".method x()V"]);
        assert!(d[0].added.is_empty());
        assert_eq!(rank_lines(&d), vec!["     1  a"]);
    }

    #[test]
    fn a_body_that_cannot_be_hashed_is_not_reported_as_changed() {
        // `None` means "could not decode", not "empty body"; treating it as a
        // difference would invent changes on every abstract or native method.
        let a = inv(&[("La;", &[(".method x()V", None)])]);
        let b = inv(&[("La;", &[(".method x()V", Some(2))])]);
        assert!(compare(&a, &b).is_empty());
    }

    #[test]
    fn a_body_that_cannot_be_hashed_on_the_new_side_is_not_reported_as_changed() {
        // The mirror of the case above, and the one that actually bit: the
        // old side hashed, the new side did not, and a decode failure was
        // reported as a method whose body had been rewritten.
        let a = inv(&[("La;", &[(".method x()V", Some(1))])]);
        let b = inv(&[("La;", &[(".method x()V", None)])]);
        assert!(compare(&a, &b).is_empty());
        // Neither does it leak into the artefacts.
        assert!(api_lines(&compare(&a, &b)).is_empty());
        assert!(norm_lines(&compare(&a, &b)).is_empty());
    }

    #[test]
    fn ranking_is_by_magnitude_then_name() {
        let a = inv(&[
            (
                "Lbig;",
                &[(".method p()V", Some(1)), (".method q()V", Some(1))],
            ),
            ("Lsmall;", &[(".method r()V", Some(1))]),
            ("Lzz;", &[(".method s()V", Some(1))]),
        ]);
        let b = inv(&[
            (
                "Lbig;",
                &[(".method p()V", Some(9)), (".method q()V", Some(9))],
            ),
            ("Lsmall;", &[(".method r()V", Some(9))]),
            ("Lzz;", &[(".method s()V", Some(9))]),
        ]);
        let d = compare(&a, &b);
        let r = rank_lines(&d);
        assert!(r[0].ends_with("big"));
        // Equal magnitude falls back to name order.
        assert!(r[1].ends_with("small"));
        assert!(r[2].ends_with("zz"));
    }

    #[test]
    fn rank_excludes_new_and_gone() {
        let a = inv(&[("La;", &[])]);
        let b = inv(&[("Lb;", &[])]);
        assert!(rank_lines(&compare(&a, &b)).is_empty());
    }

    #[test]
    fn end_to_end_on_the_generated_fixtures() {
        let old = Dex::parse(testdex::sample()).unwrap().inventory();
        let new = Dex::parse(testdex::sample_variant()).unwrap().inventory();
        let d = compare(&old, &new);
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].name, "com/example/Thing");
        assert_eq!(d[0].status, Status::Changed);
        assert_eq!(
            d[0].changed,
            vec![".method public greet(Ljava/lang/String;I)Ljava/lang/String;"],
            "only greet's body moved"
        );
        assert!(d[0].added.is_empty(), "no signature changed");
        assert_eq!(
            api_lines(&d),
            vec![
                "com/example/Thing\t~\t.method public greet(Ljava/lang/String;I)Ljava/lang/String;"
            ]
        );
        assert_eq!(norm_lines(&d), vec!["CHANGED\tcom/example/Thing"]);
    }

    #[test]
    fn delta_is_debug_printable() {
        let d = compare(&inv(&[("La;", &[])]), &inv(&[]));
        assert!(format!("{:?}", d[0]).contains("Gone"));
    }
}
