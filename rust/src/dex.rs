//! A direct reader for the Dalvik executable format.
//!
//! Stage 3 of apkdiff only ever needed four things from a dex: which classes
//! exist, what methods and fields each declares, which string literals it
//! holds, and whether a method body changed. All four live in the dex
//! structure itself — disassembling to smali text and diffing that was an
//! expensive detour through the JVM (apktool) for information the file already
//! states directly.
//!
//! One trap makes the obvious approach wrong. A method body cannot be compared
//! by hashing its raw instruction bytes: operands are indices into the string,
//! type, field and method pools, and those pools renumber whenever anything
//! else in the dex changes. Identical source would hash differently. So the
//! body signature here is the sequence of OPCODES only, which is stable under
//! pool renumbering.
//!
//! The instruction walk is self-validating: it must land exactly on the end of
//! the bytecode array. If it does not, the width table disagrees with reality
//! and the body is reported as `none` rather than as a confident wrong answer.

use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug)]
pub enum DexError {
    TooShort,
    BadMagic,
    UnsupportedEndian,
    Truncated(&'static str),
}

impl std::fmt::Display for DexError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DexError::TooShort => write!(f, "file too short to be a dex"),
            DexError::BadMagic => write!(f, "not a dex file (bad magic)"),
            DexError::UnsupportedEndian => write!(f, "big-endian dex is not supported"),
            DexError::Truncated(what) => write!(f, "dex truncated while reading {what}"),
        }
    }
}

pub struct Dex {
    data: Vec<u8>,
    strings: Vec<String>,
    type_ids: Vec<u32>,
    proto_ids: Vec<ProtoId>,
    field_ids: Vec<FieldId>,
    method_ids: Vec<MethodId>,
    class_defs: Vec<ClassDef>,
}

struct ProtoId {
    return_type_idx: u32,
    parameters_off: u32,
}

struct FieldId {
    type_idx: u16,
    name_idx: u32,
}

struct MethodId {
    proto_idx: u16,
    name_idx: u32,
}

struct ClassDef {
    class_idx: u32,
    class_data_off: u32,
}

/// One declared member, rendered in smali syntax so downstream tooling that
/// already parses `.method`/`.field` lines keeps working unchanged.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Member {
    pub line: String,
    /// Opcode-sequence hash of the body; `None` for fields and abstract or
    /// native methods, which have no bytecode.
    pub body: Option<u64>,
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Class {
    pub members: Vec<Member>,
    /// The string literals this class's own code loads, quoted as they appear
    /// in a report. The dex string pool is global — it says which strings the
    /// file contains but not which code uses them — so a new literal could not
    /// be attributed to anything until these were collected during the same
    /// instruction walk that hashes the bodies.
    pub literals: BTreeSet<String>,
}

fn u16le(d: &[u8], off: usize) -> Result<u16, DexError> {
    d.get(off..off + 2)
        .map(|s| u16::from_le_bytes([s[0], s[1]]))
        .ok_or(DexError::Truncated("u16"))
}

fn u32le(d: &[u8], off: usize) -> Result<u32, DexError> {
    d.get(off..off + 4)
        .map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
        .ok_or(DexError::Truncated("u32"))
}

/// LEB128, unsigned. Returns the value and the number of bytes consumed.
pub fn uleb128(d: &[u8], off: usize) -> Result<(u32, usize), DexError> {
    let mut result: u32 = 0;
    let mut shift = 0;
    let mut n = 0;
    loop {
        let b = *d.get(off + n).ok_or(DexError::Truncated("uleb128"))?;
        result |= ((b & 0x7f) as u32) << shift;
        n += 1;
        if b & 0x80 == 0 {
            break;
        }
        shift += 7;
        if shift > 28 {
            break;
        }
    }
    Ok((result, n))
}

impl Dex {
    pub fn parse(data: Vec<u8>) -> Result<Dex, DexError> {
        if data.len() < 112 {
            return Err(DexError::TooShort);
        }
        if &data[0..4] != b"dex\n" {
            return Err(DexError::BadMagic);
        }
        // endian_tag: 0x12345678 little-endian, 0x78563412 means byte-swapped.
        if u32le(&data, 40)? != 0x1234_5678 {
            return Err(DexError::UnsupportedEndian);
        }

        let string_ids_size = u32le(&data, 56)? as usize;
        let string_ids_off = u32le(&data, 60)? as usize;
        let type_ids_size = u32le(&data, 64)? as usize;
        let type_ids_off = u32le(&data, 68)? as usize;
        let proto_ids_size = u32le(&data, 72)? as usize;
        let proto_ids_off = u32le(&data, 76)? as usize;
        let field_ids_size = u32le(&data, 80)? as usize;
        let field_ids_off = u32le(&data, 84)? as usize;
        let method_ids_size = u32le(&data, 88)? as usize;
        let method_ids_off = u32le(&data, 92)? as usize;
        let class_defs_size = u32le(&data, 96)? as usize;
        let class_defs_off = u32le(&data, 100)? as usize;

        let mut strings = Vec::with_capacity(string_ids_size);
        for i in 0..string_ids_size {
            let off = u32le(&data, string_ids_off + i * 4)? as usize;
            // string_data_item: uleb128 utf16 length, then MUTF-8 up to NUL.
            let (_, n) = uleb128(&data, off)?;
            let start = off + n;
            let end = data[start..]
                .iter()
                .position(|&b| b == 0)
                .map(|p| start + p)
                .ok_or(DexError::Truncated("string data"))?;
            strings.push(String::from_utf8_lossy(&data[start..end]).into_owned());
        }

        let mut type_ids = Vec::with_capacity(type_ids_size);
        for i in 0..type_ids_size {
            type_ids.push(u32le(&data, type_ids_off + i * 4)?);
        }

        let mut proto_ids = Vec::with_capacity(proto_ids_size);
        for i in 0..proto_ids_size {
            let base = proto_ids_off + i * 12;
            proto_ids.push(ProtoId {
                return_type_idx: u32le(&data, base + 4)?,
                parameters_off: u32le(&data, base + 8)?,
            });
        }

        let mut field_ids = Vec::with_capacity(field_ids_size);
        for i in 0..field_ids_size {
            let base = field_ids_off + i * 8;
            field_ids.push(FieldId {
                type_idx: u16le(&data, base + 2)?,
                name_idx: u32le(&data, base + 4)?,
            });
        }

        let mut method_ids = Vec::with_capacity(method_ids_size);
        for i in 0..method_ids_size {
            let base = method_ids_off + i * 8;
            method_ids.push(MethodId {
                proto_idx: u16le(&data, base + 2)?,
                name_idx: u32le(&data, base + 4)?,
            });
        }

        let mut class_defs = Vec::with_capacity(class_defs_size);
        for i in 0..class_defs_size {
            let base = class_defs_off + i * 32;
            class_defs.push(ClassDef {
                class_idx: u32le(&data, base)?,
                class_data_off: u32le(&data, base + 24)?,
            });
        }

        Ok(Dex {
            data,
            strings,
            type_ids,
            proto_ids,
            field_ids,
            method_ids,
            class_defs,
        })
    }

    fn string(&self, idx: u32) -> &str {
        self.strings
            .get(idx as usize)
            .map(|s| s.as_str())
            .unwrap_or("?")
    }

    fn type_name(&self, idx: u32) -> &str {
        match self.type_ids.get(idx as usize) {
            Some(&s) => self.string(s),
            None => "?",
        }
    }

    /// Every string literal in the pool, quoted, filtered to the same shape the
    /// previous smali-scraping path produced: 4..160 characters, no embedded
    /// double quote (which that regex could not have matched either).
    pub fn literals(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .strings
            .iter()
            .filter(|s| Self::quotable(s))
            .map(|s| format!("\"{s}\""))
            .collect();
        out.sort();
        out.dedup();
        out
    }

    fn proto_sig(&self, proto_idx: u32) -> String {
        let Some(p) = self.proto_ids.get(proto_idx as usize) else {
            return "()?".to_string();
        };
        let mut params = String::new();
        if p.parameters_off != 0 {
            let off = p.parameters_off as usize;
            if let Ok(size) = u32le(&self.data, off) {
                for i in 0..size as usize {
                    if let Ok(t) = u16le(&self.data, off + 4 + i * 2) {
                        params.push_str(self.type_name(t as u32));
                    }
                }
            }
        }
        format!("({params}){}", self.type_name(p.return_type_idx))
    }

    /// Build the class -> members map. Members are rendered as smali
    /// declaration lines and sorted, which is exactly what a signature-level
    /// diff needs.
    pub fn inventory(&self) -> BTreeMap<String, Class> {
        let mut out: BTreeMap<String, Class> = BTreeMap::new();
        for cd in &self.class_defs {
            let name = self.type_name(cd.class_idx).to_string();
            let entry = out.entry(name).or_default();
            if cd.class_data_off != 0 {
                let _ = self.read_class_data(cd.class_data_off as usize, entry);
            }
            entry.members.sort();
            entry.members.dedup();
        }
        out
    }

    fn read_class_data(&self, off: usize, out: &mut Class) -> Result<(), DexError> {
        let d = &self.data;
        let mut p = off;
        let take = |p: &mut usize| -> Result<u32, DexError> {
            let (v, n) = uleb128(d, *p)?;
            *p += n;
            Ok(v)
        };
        let static_fields = take(&mut p)?;
        let instance_fields = take(&mut p)?;
        let direct_methods = take(&mut p)?;
        let virtual_methods = take(&mut p)?;

        for count in [static_fields, instance_fields] {
            let mut idx: u32 = 0;
            for i in 0..count {
                let diff = take(&mut p)?;
                let access = take(&mut p)?;
                idx = if i == 0 { diff } else { idx + diff };
                if let Some(f) = self.field_ids.get(idx as usize) {
                    out.members.push(Member {
                        line: format!(
                            ".field {}{}:{}",
                            access_flags(access, false),
                            self.string(f.name_idx),
                            self.type_name(f.type_idx as u32)
                        ),
                        body: None,
                    });
                }
            }
        }

        let mut literals = BTreeSet::new();
        for count in [direct_methods, virtual_methods] {
            let mut idx: u32 = 0;
            for i in 0..count {
                let diff = take(&mut p)?;
                let access = take(&mut p)?;
                let code_off = take(&mut p)?;
                idx = if i == 0 { diff } else { idx + diff };
                if let Some(m) = self.method_ids.get(idx as usize) {
                    let name = self.string(m.name_idx);
                    let is_ctor = name == "<init>" || name == "<clinit>";
                    out.members.push(Member {
                        line: format!(
                            ".method {}{}{}",
                            access_flags(access, is_ctor),
                            name,
                            self.proto_sig(m.proto_idx as u32)
                        ),
                        body: if code_off == 0 {
                            None
                        } else {
                            self.scan_body(code_off as usize, &mut literals)
                        },
                    });
                }
            }
        }
        out.literals.append(&mut literals);
        Ok(())
    }

    /// Hash the opcode sequence of one method body.
    ///
    /// Operands are deliberately excluded: they hold pool indices that shift
    /// whenever anything else in the dex changes, so including them would
    /// report every method as modified.
    /// Is this string worth reporting? The pool holds every type descriptor,
    /// method name and annotation value as well as user-visible text.
    fn quotable(s: &str) -> bool {
        s.len() >= 4 && s.len() <= 160 && !s.contains('"')
    }

    /// Walk one method body: hash its opcode sequence and collect the string
    /// literals it loads. Both come from the same pass because both need the
    /// instruction widths, and walking twice would double the cost of the
    /// most expensive thing the reader does.
    fn scan_body(&self, code_off: usize, seen: &mut BTreeSet<String>) -> Option<u64> {
        let d = &self.data;
        // code_item: registers_size, ins_size, outs_size, tries_size (u16 x4),
        // debug_info_off (u32), insns_size (u32), then insns[insns_size] u16.
        let insns_size = u32le(d, code_off + 12).ok()? as usize;
        let start = code_off + 16;
        let end = start + insns_size * 2;
        let insns = d.get(start..end)?;

        let mut h: u64 = 0xcbf2_9ce4_8422_2325; // FNV-1a offset basis
        let mut i = 0usize; // in code units
        while i < insns_size {
            let unit = u16::from_le_bytes([insns[i * 2], insns[i * 2 + 1]]);
            let op = (unit & 0xff) as u8;
            let width = instruction_width(op, unit, insns, i, insns_size)?;
            // Overrunning the array means the width table disagrees with the
            // file. Every width is >= 1, so this is also what guarantees the
            // walk terminates on the exact end rather than past it.
            if i + width > insns_size {
                return None;
            }
            h ^= op as u64;
            h = h.wrapping_mul(0x100_0000_01b3);
            // const-string vAA, string@BBBB / const-string/jumbo string@BBBBBBBB.
            // The operand is a pool index, which is why it is deliberately kept
            // out of the hash and read separately here.
            let idx = match op {
                0x1a => u16le(insns, i * 2 + 2).ok().map(u32::from),
                0x1b => u32le(insns, i * 2 + 2).ok(),
                _ => None,
            };
            if let Some(s) = idx.and_then(|k| self.strings.get(k as usize)) {
                if Self::quotable(s) {
                    seen.insert(format!("\"{s}\""));
                }
            }
            i += width;
        }
        Some(h)
    }
}

/// Width of one instruction, in 16-bit code units.
///
/// Derived from the Dalvik instruction formats: the width is a property of the
/// opcode alone, except for the three payload pseudo-instructions that carry
/// their own length.
pub fn instruction_width(
    op: u8,
    unit: u16,
    insns: &[u8],
    i: usize,
    insns_size: usize,
) -> Option<usize> {
    if op == 0x00 && unit != 0x0000 {
        let ident = unit >> 8;
        let read_u16 = |k: usize| -> Option<u16> {
            if k < insns_size {
                Some(u16::from_le_bytes([insns[k * 2], insns[k * 2 + 1]]))
            } else {
                None
            }
        };
        let read_u32 = |k: usize| -> Option<u32> {
            Some(u32::from(read_u16(k)?) | (u32::from(read_u16(k + 1)?) << 16))
        };
        return match ident {
            1 => Some(read_u16(i + 1)? as usize * 2 + 4), // packed-switch-payload
            2 => Some(read_u16(i + 1)? as usize * 4 + 2), // sparse-switch-payload
            3 => {
                // fill-array-data-payload
                let element_width = read_u16(i + 1)? as usize;
                let size = read_u32(i + 2)? as usize;
                Some((size * element_width).div_ceil(2) + 4)
            }
            _ => None,
        };
    }
    Some(match op {
        0x00 | 0x01 | 0x04 | 0x07 | 0x0a..=0x12 | 0x1d | 0x1e | 0x21 | 0x27 | 0x28 => 1,
        0x02 | 0x05 | 0x08 | 0x13 | 0x15 | 0x16 | 0x19 | 0x1a | 0x1c | 0x1f | 0x20 | 0x22
        | 0x23 | 0x29 => 2,
        0x03 | 0x06 | 0x09 | 0x14 | 0x17 | 0x1b | 0x24..=0x26 | 0x2a..=0x2c => 3,
        0x18 => 5,
        0x2d..=0x3d => 2,
        0x3e..=0x43 => 1, // unused
        0x44..=0x6d => 2,
        0x6e..=0x72 => 3,
        0x73 => 1, // unused
        0x74..=0x78 => 3,
        0x79 | 0x7a => 1, // unused
        0x7b..=0x8f => 1,
        0x90..=0xaf => 2,
        0xb0..=0xcf => 1,
        0xd0..=0xe2 => 2,
        0xe3..=0xf9 => 1, // unused
        0xfa | 0xfb => 4,
        0xfc | 0xfd => 3,
        0xfe | 0xff => 2,
    })
}

/// Smali modifier prefix, in the order smali itself emits them.
pub fn access_flags(access: u32, is_ctor: bool) -> String {
    const NAMES: [(u32, &str); 13] = [
        (0x0001, "public"),
        (0x0002, "private"),
        (0x0004, "protected"),
        (0x0008, "static"),
        (0x0010, "final"),
        (0x0020, "synchronized"),
        (0x0040, "bridge"),
        (0x0080, "varargs"),
        (0x0100, "native"),
        (0x0400, "abstract"),
        (0x0800, "strictfp"),
        (0x1000, "synthetic"),
        (0x0020_0000, "declared-synchronized"),
    ];
    let mut out = String::new();
    for (bit, name) in NAMES {
        // 0x0020 is `synchronized` on methods but `volatile` on fields; only
        // methods reach here with it set in practice, and the label is only
        // ever compared against itself, so the distinction does not matter.
        if access & bit != 0 {
            out.push_str(name);
            out.push(' ');
        }
    }
    if is_ctor {
        out.push_str("constructor ");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testdex;

    #[test]
    fn uleb128_single_and_multi_byte() {
        assert_eq!(uleb128(&[0x00], 0).unwrap(), (0, 1));
        assert_eq!(uleb128(&[0x7f], 0).unwrap(), (127, 1));
        assert_eq!(uleb128(&[0x80, 0x01], 0).unwrap(), (128, 2));
        assert_eq!(uleb128(&[0xff, 0x7f], 0).unwrap(), (16383, 2));
        assert_eq!(uleb128(&[0x80, 0x80, 0x80, 0x80, 0x08], 0).unwrap().1, 5);
        assert!(uleb128(&[0x80], 0).is_err());
        // Overlong encodings bail out rather than shifting off the end of u32.
        assert_eq!(
            uleb128(&[0x80, 0x80, 0x80, 0x80, 0x80, 0x01], 0).unwrap().1,
            5
        );
    }

    #[test]
    fn integer_readers_bounds_check() {
        assert_eq!(u16le(&[0x34, 0x12], 0).unwrap(), 0x1234);
        assert_eq!(u32le(&[0x78, 0x56, 0x34, 0x12], 0).unwrap(), 0x1234_5678);
        assert!(u16le(&[0x00], 0).is_err());
        assert!(u32le(&[0x00, 0x00], 0).is_err());
    }

    #[test]
    fn rejects_non_dex_input() {
        assert!(matches!(Dex::parse(vec![0u8; 8]), Err(DexError::TooShort)));
        assert!(matches!(
            Dex::parse(vec![0u8; 200]),
            Err(DexError::BadMagic)
        ));
        let mut d = vec![0u8; 200];
        d[0..4].copy_from_slice(b"dex\n");
        assert!(matches!(Dex::parse(d), Err(DexError::UnsupportedEndian)));
    }

    #[test]
    fn error_messages_render() {
        for e in [
            DexError::TooShort,
            DexError::BadMagic,
            DexError::UnsupportedEndian,
            DexError::Truncated("thing"),
        ] {
            assert!(!e.to_string().is_empty());
        }
        assert!(format!("{:?}", DexError::BadMagic).contains("BadMagic"));
    }

    #[test]
    fn parses_a_synthetic_dex() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        let inv = dex.inventory();
        let c = inv.get("Lcom/example/Thing;").expect("class present");
        let lines: Vec<&str> = c.members.iter().map(|m| m.line.as_str()).collect();
        assert!(lines
            .iter()
            .any(|l| l.contains(".field private final counter:I")));
        assert!(lines.iter().any(|l| l.contains(".method public run()V")));
        assert!(lines
            .iter()
            .any(|l| l.contains(".method public greet(Ljava/lang/String;I)Ljava/lang/String;")));
    }

    #[test]
    fn literals_are_filtered_and_quoted() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        let lits = dex.literals();
        assert!(lits.contains(&"\"hello world\"".to_string()));
        // "I" and "V" are shorter than the 4-character floor.
        assert!(!lits.iter().any(|l| l == "\"I\""));
        assert!(
            lits.windows(2).all(|w| w[0] <= w[1]),
            "literals must be sorted"
        );
    }

    #[test]
    fn body_scan_is_stable_and_present_for_real_code() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        let inv = dex.inventory();
        let c = &inv["Lcom/example/Thing;"];
        let run = c
            .members
            .iter()
            .find(|m| m.line.contains("run()V"))
            .unwrap();
        assert!(run.body.is_some(), "run() has bytecode so it must hash");
        let again = Dex::parse(testdex::sample()).unwrap().inventory();
        assert_eq!(
            run.body,
            again["Lcom/example/Thing;"]
                .members
                .iter()
                .find(|m| m.line.contains("run()V"))
                .unwrap()
                .body
        );
    }

    #[test]
    fn abstract_method_has_no_body() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        let inv = dex.inventory();
        let c = &inv["Lcom/example/Thing;"];
        let a = c
            .members
            .iter()
            .find(|m| m.line.contains("abstractly"))
            .unwrap();
        assert!(a.body.is_none());
        assert!(a.line.contains("abstract"));
    }

    #[test]
    fn truncation_is_reported_never_panicked() {
        // Every table read is bounds-checked; walking the fixture's whole
        // length proves no offset path can panic on a short file.
        let full = testdex::sample();
        let mut rejected = 0;
        for cut in 112..full.len() {
            match Dex::parse(full[..cut].to_vec()) {
                Ok(d) => {
                    let _ = d.inventory();
                    let _ = d.literals();
                }
                Err(_) => rejected += 1,
            }
        }
        assert!(rejected > 0, "a truncated dex must be rejected somewhere");
    }

    #[test]
    fn members_referencing_missing_ids_are_skipped() {
        // The fixture declares one field and one method entry whose pool index
        // does not exist. They must vanish rather than render as "?".
        let dex = Dex::parse(testdex::sample()).unwrap();
        let c = &dex.inventory()["Lcom/example/Thing;"];
        assert_eq!(
            c.members
                .iter()
                .filter(|m| m.line.starts_with(".field"))
                .count(),
            1
        );
        assert_eq!(
            c.members
                .iter()
                .filter(|m| m.line.starts_with(".method"))
                .count(),
            5
        );
        assert!(!c.members.iter().any(|m| m.line.contains("?")));
    }

    #[test]
    fn unreadable_parameter_lists_yield_an_empty_signature() {
        // proto 2 points past EOF; proto 3 claims more type ids than exist.
        // Neither may abort the parse or invent parameters.
        let dex = Dex::parse(testdex::sample()).unwrap();
        let c = &dex.inventory()["Lcom/example/Thing;"];
        let lines: Vec<&str> = c.members.iter().map(|m| m.line.as_str()).collect();
        assert!(lines.contains(&".method public brokenparams()V"));
        assert!(lines.contains(&".method public brokenlist()V"));
    }

    #[test]
    fn unknown_type_and_string_indices_degrade_gracefully() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        assert_eq!(dex.string(9_999), "?");
        assert_eq!(dex.type_name(9_999), "?");
        assert_eq!(dex.proto_sig(9_999), "()?");
    }

    #[test]
    fn access_flag_rendering() {
        assert_eq!(access_flags(0x0001, false), "public ");
        assert_eq!(access_flags(0x0009, false), "public static ");
        assert_eq!(access_flags(0x0001, true), "public constructor ");
        assert_eq!(access_flags(0x0000, false), "");
        assert_eq!(access_flags(0x0020_0000, false), "declared-synchronized ");
        assert!(access_flags(0x1000, false).contains("synthetic"));
        assert!(access_flags(0x0800, false).contains("strictfp"));
        assert!(access_flags(0x0400, false).contains("abstract"));
        assert!(access_flags(0x0100, false).contains("native"));
        assert!(access_flags(0x0080, false).contains("varargs"));
        assert!(access_flags(0x0040, false).contains("bridge"));
        assert!(access_flags(0x0004, false).contains("protected"));
        assert!(access_flags(0x0002, false).contains("private"));
        assert!(access_flags(0x0010, false).contains("final"));
    }

    #[test]
    fn instruction_widths_cover_every_opcode() {
        // Every non-payload opcode must yield a width of at least one unit.
        for op in 0u16..=0xff {
            let w = instruction_width(op as u8, op, &[], 0, 0);
            assert!(w.unwrap() >= 1, "opcode {op:#04x} has no width");
        }
        assert_eq!(instruction_width(0x00, 0x0000, &[], 0, 0), Some(1)); // nop
        assert_eq!(instruction_width(0x18, 0x0018, &[], 0, 0), Some(5)); // const-wide
        assert_eq!(instruction_width(0x6e, 0x006e, &[], 0, 0), Some(3)); // invoke-virtual
        assert_eq!(instruction_width(0xfa, 0x00fa, &[], 0, 0), Some(4)); // invoke-polymorphic
    }

    #[test]
    fn payload_widths_are_read_from_the_payload() {
        // packed-switch-payload: ident 1, size 3 -> 3*2 + 4 = 10 units
        let insns: Vec<u8> = vec![0x00, 0x01, 0x03, 0x00];
        assert_eq!(instruction_width(0x00, 0x0100, &insns, 0, 2), Some(10));
        // sparse-switch-payload: ident 2, size 2 -> 2*4 + 2 = 10 units
        let insns: Vec<u8> = vec![0x00, 0x02, 0x02, 0x00];
        assert_eq!(instruction_width(0x00, 0x0200, &insns, 0, 2), Some(10));
        // fill-array-data-payload: ident 3, width 1, size 5 -> (5+1)/2 + 4 = 7
        let insns: Vec<u8> = vec![0x00, 0x03, 0x01, 0x00, 0x05, 0x00, 0x00, 0x00];
        assert_eq!(instruction_width(0x00, 0x0300, &insns, 0, 4), Some(7));
        // Unknown payload identifier.
        assert_eq!(instruction_width(0x00, 0x0400, &insns, 0, 4), None);
        // Truncated payload.
        assert_eq!(instruction_width(0x00, 0x0100, &[0x00, 0x01], 0, 1), None);
    }

    #[test]
    fn literals_are_attributed_to_the_class_that_loads_them() {
        for bytes in [testdex::sample(), testdex::sample_jumbo()] {
            let inv = Dex::parse(bytes).unwrap().inventory();
            let thing = &inv["Lcom/example/Thing;"];
            assert!(
                thing.literals.contains("\"hello world\""),
                "const-string and its jumbo form both name the string"
            );
            assert!(
                inv["Lcom/example/Empty;"].literals.is_empty(),
                "a class with no code loads nothing"
            );
        }
    }

    #[test]
    fn body_scan_rejects_a_walk_that_does_not_land_exactly() {
        // A code_item whose single instruction claims 3 units but only 2 exist.
        let mut d = testdex::sample();
        let bad = testdex::code_item_with_insns(&[0x6e, 0x00]); // invoke-virtual, width 3
        let off = d.len();
        d.extend_from_slice(&bad);
        let dex = Dex::parse(d).unwrap();
        assert_eq!(dex.scan_body(off, &mut BTreeSet::new()), None);
    }

    #[test]
    fn body_scan_handles_truncated_code_item() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        assert_eq!(dex.scan_body(usize::MAX - 32, &mut BTreeSet::new()), None);
    }

    #[test]
    fn class_without_data_yields_no_members() {
        let dex = Dex::parse(testdex::sample()).unwrap();
        let inv = dex.inventory();
        assert!(inv["Lcom/example/Empty;"].members.is_empty());
    }

    #[test]
    fn member_ordering_is_total() {
        let a = Member {
            line: ".field a:I".into(),
            body: None,
        };
        let b = Member {
            line: ".field b:I".into(),
            body: None,
        };
        assert!(a < b);
        assert_eq!(a.clone(), a);
        assert!(format!("{a:?}").contains("field"));
        assert_eq!(Class::default().members.len(), 0);
    }
}
