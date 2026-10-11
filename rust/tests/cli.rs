//! End-to-end tests for the two binaries: argument handling, exit codes, the
//! files they write, and the exact text they print.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn tmpdir(tag: &str) -> PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!("apkdiff-test-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&p);
    std::fs::create_dir_all(&p).unwrap();
    p
}

fn run(bin: &str, args: &[&str]) -> Output {
    Command::new(bin).args(args).output().expect("binary runs")
}

const NATIVE: &str = env!("CARGO_BIN_EXE_apkdiff-native-strings");
const DEX: &str = env!("CARGO_BIN_EXE_apkdiff-dex");

fn write(dir: &Path, name: &str, bytes: &[u8]) -> String {
    let p = dir.join(name);
    std::fs::write(&p, bytes).unwrap();
    p.to_str().unwrap().to_string()
}

// ---------------------------------------------------------------- native-strings

#[test]
fn native_reports_usage_without_enough_arguments() {
    let out = run(NATIVE, &[]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("usage"));
}

#[test]
fn native_reports_a_missing_file() {
    let out = run(NATIVE, &["/nonexistent/a.so", "/nonexistent/b.so"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("/nonexistent/a.so"));
}

#[test]
fn native_reports_a_missing_second_file() {
    let d = tmpdir("native-second");
    let a = write(&d, "a.so", b"\x00the quick brown fox jumps\x00");
    let out = run(NATIVE, &[&a, "/nonexistent/b.so"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("/nonexistent/b.so"));
}

#[test]
fn native_finds_an_added_message_and_spills_it() {
    let d = tmpdir("native-add");
    let a = write(&d, "old.so", b"\x00the quick brown fox jumps\x00");
    let b = write(
        &d,
        "new.so",
        b"\x00the quick brown fox jumps\x00could not tell the service it failed\x00",
    );
    let prefix = d.join("spill").to_str().unwrap().to_string();
    let out = run(NATIVE, &[&a, &b, &prefix]);
    assert!(out.status.success());
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("=== ADDED strings (1) ==="));
    assert!(text.contains("+ could not tell the service it failed"));
    assert!(text.contains("=== REMOVED strings (0) ==="));
    assert!(text.contains("@0x"), "context offset is printed");

    let spilled = std::fs::read_to_string(format!("{prefix}.added.txt")).unwrap();
    assert_eq!(spilled.trim(), "could not tell the service it failed");
    assert_eq!(
        std::fs::read_to_string(format!("{prefix}.removed.txt")).unwrap(),
        ""
    );
}

#[test]
fn native_verify_accepts_a_matching_abi() {
    let d = tmpdir("native-verify-ok");
    let a = write(&d, "old.so", b"\x00the quick brown fox jumps\x00");
    let b = write(
        &d,
        "new.so",
        b"\x00the quick brown fox jumps\x00could not tell the service it failed\x00",
    );
    let prefix = d.join("spill").to_str().unwrap().to_string();
    assert!(run(NATIVE, &[&a, &b, &prefix]).status.success());

    let out = run(NATIVE, &["--verify", &a, &b, &prefix]);
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("verified against primary ABI: OK"));
}

#[test]
fn native_verify_flags_a_diverging_abi() {
    let d = tmpdir("native-verify-bad");
    let a = write(&d, "old.so", b"\x00identical content here\x00");
    let b = write(&d, "new.so", b"\x00identical content here\x00");
    let prefix = d.join("spill").to_str().unwrap().to_string();
    std::fs::write(format!("{prefix}.added.txt"), "a phrase not present\n").unwrap();
    std::fs::write(format!("{prefix}.removed.txt"), "").unwrap();

    let out = run(NATIVE, &["--verify", &a, &b, &prefix]);
    assert!(out.status.success());
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("MISMATCH (added)"));
    assert!(text.contains("1 mismatch(es)"));
}

#[test]
fn native_verify_requires_a_spill_prefix() {
    let d = tmpdir("native-verify-noprefix");
    let a = write(&d, "a.so", b"\x00abc\x00");
    let out = run(NATIVE, &["--verify", &a, &a]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("spill prefix"));
}

#[test]
fn native_verify_tolerates_absent_spill_files() {
    let d = tmpdir("native-verify-nospill");
    let a = write(&d, "a.so", b"\x00abc\x00");
    let out = run(NATIVE, &["--verify", &a, &a, "/nonexistent/prefix"]);
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("OK"));
}

#[test]
fn native_reports_removed_text_too() {
    let d = tmpdir("native-removed");
    let a = write(&d, "old.so", b"\x00this message went away entirely\x00");
    let b = write(&d, "new.so", b"\x00\x00");
    let out = run(NATIVE, &[&a, &b]);
    assert!(out.status.success());
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("=== REMOVED strings (1) ==="));
    assert!(text.contains("- this message went away entirely"));
}

#[test]
fn native_spill_path_errors_are_reported() {
    let d = tmpdir("native-spill-fail");
    let a = write(&d, "a.so", b"\x00abc\x00");
    let out = run(NATIVE, &[&a, &a, "/nonexistent-dir/prefix"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("nonexistent-dir"));
}

// ---------------------------------------------------------------- dex

fn fixtures(dir: &Path) -> (String, String) {
    (
        write(dir, "old.dex", &apkdiff::testdex::sample()),
        write(dir, "new.dex", &apkdiff::testdex::sample_variant()),
    )
}

#[test]
fn dex_reports_usage_without_enough_arguments() {
    let out = run(DEX, &["only-one"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("usage"));
}

#[test]
fn dex_reports_an_unreadable_file() {
    let out = run(DEX, &["/nonexistent/a.dex", "/nonexistent/b.dex", "/tmp/p"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("/nonexistent/a.dex"));
}

#[test]
fn dex_reports_a_file_that_is_not_a_dex() {
    let d = tmpdir("dex-bad");
    let bad = write(&d, "bad.dex", &vec![0u8; 300]);
    let out = run(DEX, &[&bad, &bad, d.join("p").to_str().unwrap()]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("bad magic"));
}

#[test]
fn dex_reports_an_unreadable_second_file() {
    let d = tmpdir("dex-second");
    let (old, _) = fixtures(&d);
    let out = run(
        DEX,
        &[&old, "/nonexistent/b.dex", d.join("p").to_str().unwrap()],
    );
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("/nonexistent/b.dex"));
}

#[test]
fn dex_writes_every_artefact_and_finds_the_body_change() {
    let d = tmpdir("dex-ok");
    let (old, new) = fixtures(&d);
    let prefix = d.join("out").to_str().unwrap().to_string();
    let out = run(DEX, &[&old, &new, &prefix]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );

    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("classes: 1 changed, 0 new, 0 removed"));
    assert!(text.contains("members: 0 added, 0 removed"));

    let norm = std::fs::read_to_string(format!("{prefix}.norm")).unwrap();
    assert_eq!(norm.trim(), "CHANGED\tcom/example/Thing");

    // No signature moved, so the only member row is the rewritten body.
    assert_eq!(
        std::fs::read_to_string(format!("{prefix}.api")).unwrap(),
        "com/example/Thing\t~\t.method public greet(Ljava/lang/String;I)Ljava/lang/String;\n"
    );

    let rank = std::fs::read_to_string(format!("{prefix}.rank")).unwrap();
    assert!(rank.contains("com/example/Thing"));

    for side in ["old", "new"] {
        let lits = std::fs::read_to_string(format!("{prefix}.str.{side}")).unwrap();
        assert!(lits.contains("\"hello world\""), "{side} literals");
    }
}

#[test]
fn dex_comparing_a_file_to_itself_finds_nothing() {
    let d = tmpdir("dex-same");
    let (old, _) = fixtures(&d);
    let prefix = d.join("out").to_str().unwrap().to_string();
    let out = run(DEX, &[&old, &old, &prefix]);
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("classes: 0 changed, 0 new, 0 removed"));
    assert_eq!(
        std::fs::read_to_string(format!("{prefix}.norm")).unwrap(),
        ""
    );
}

#[test]
fn dex_reports_an_unwritable_output_prefix() {
    let d = tmpdir("dex-unwritable");
    let (old, new) = fixtures(&d);
    let out = run(DEX, &[&old, &new, "/nonexistent-dir/prefix"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("nonexistent-dir"));
}

#[test]
fn dex_emits_a_fixture_for_the_shell_suite() {
    let d = tmpdir("dex-fixture");
    let a = d.join("a.dex");
    let b = d.join("b.dex");
    assert!(run(DEX, &["--emit-fixture", a.to_str().unwrap()])
        .status
        .success());
    assert!(
        run(DEX, &["--emit-fixture", b.to_str().unwrap(), "--variant"])
            .status
            .success()
    );
    let (ba, bb) = (std::fs::read(&a).unwrap(), std::fs::read(&b).unwrap());
    assert_eq!(&ba[0..4], b"dex\n");
    assert_ne!(ba, bb, "the variant must differ");

    // And the emitted pair must be diffable by the tool itself.
    let prefix = d.join("out").to_str().unwrap().to_string();
    let out = run(DEX, &[a.to_str().unwrap(), b.to_str().unwrap(), &prefix]);
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("1 changed"));
}

#[test]
fn dex_emit_fixture_requires_a_path() {
    let out = run(DEX, &["--emit-fixture"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("--emit-fixture PATH"));
}

#[test]
fn dex_emit_fixture_reports_an_unwritable_path() {
    let out = run(DEX, &["--emit-fixture", "/nonexistent-dir/x.dex"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("nonexistent-dir"));
}

// ---------------------------------------------------------------- pooling

#[test]
fn dex_pool_merges_both_sides_and_hides_a_class_that_only_moved() {
    // The same two classes on each side, split across shards differently:
    // exactly what R8 re-sharding looks like. Pooled, nothing moved.
    let d = tmpdir("dex-pool");
    let both = write(&d, "both.dex", &apkdiff::testdex::sample());
    let empty = write(&d, "empty.dex", &apkdiff::testdex::sample_empty());
    let prefix = d.join("out").to_str().unwrap().to_string();
    let out = run(
        DEX,
        &[
            "--pool", &prefix, "--old", &both, &empty, "--new", &empty, &both,
        ],
    );
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        text.contains("classes: 0 changed, 0 new, 0 removed"),
        "a class that only changed shard must not read as new/removed: {text}"
    );
    assert!(text.contains("pooled 2 old and 2 new dex file(s)"));
    assert_eq!(
        std::fs::read_to_string(format!("{prefix}.norm")).unwrap(),
        ""
    );
}

#[test]
fn dex_pool_unions_the_string_literals() {
    let d = tmpdir("dex-pool-str");
    let both = write(&d, "both.dex", &apkdiff::testdex::sample());
    let empty = write(&d, "empty.dex", &apkdiff::testdex::sample_empty());
    let prefix = d.join("out").to_str().unwrap().to_string();
    assert!(run(
        DEX,
        &["--pool", &prefix, "--old", &empty, "--new", &both, &empty]
    )
    .status
    .success());
    let lits = std::fs::read_to_string(format!("{prefix}.str.new")).unwrap();
    assert!(lits.contains("\"hello world\""));
    // Sorted and deduplicated across the pool.
    let rows: Vec<&str> = lits.lines().collect();
    let mut sorted = rows.clone();
    sorted.sort_unstable();
    sorted.dedup();
    assert_eq!(rows, sorted);
}

#[test]
fn dex_pool_still_finds_a_real_change() {
    let d = tmpdir("dex-pool-real");
    let (old, new) = fixtures(&d);
    let empty = write(&d, "empty.dex", &apkdiff::testdex::sample_empty());
    let prefix = d.join("out").to_str().unwrap().to_string();
    let out = run(
        DEX,
        &["--pool", &prefix, "--old", &old, "--new", &new, &empty],
    );
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("1 changed"));
}

#[test]
fn dex_pool_requires_a_prefix() {
    let out = run(DEX, &["--pool"]);
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("--pool OUT_PREFIX"));
}

#[test]
fn dex_pool_requires_both_sides() {
    let d = tmpdir("dex-pool-oneside");
    let (old, _) = fixtures(&d);
    let out = run(
        DEX,
        &["--pool", d.join("p").to_str().unwrap(), "--old", &old],
    );
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("at least one dex on each side"));
}

#[test]
fn dex_pool_rejects_a_path_before_either_side_flag() {
    let d = tmpdir("dex-pool-stray");
    let (old, _) = fixtures(&d);
    let out = run(
        DEX,
        &["--pool", d.join("p").to_str().unwrap(), &old, "--new", &old],
    );
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("came before --old or --new"));
}

#[test]
fn dex_emits_an_empty_fixture_for_shard_tests() {
    let d = tmpdir("dex-empty-fixture");
    let e = d.join("empty.dex");
    assert!(
        run(DEX, &["--emit-fixture", e.to_str().unwrap(), "--empty"])
            .status
            .success()
    );
    let full = write(&d, "full.dex", &apkdiff::testdex::sample());
    let prefix = d.join("out").to_str().unwrap().to_string();
    // Nothing in, everything out: the empty shard must parse, not error.
    let out = run(DEX, &[e.to_str().unwrap(), &full, &prefix]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("0 changed, 2 new, 0 removed"));
}
