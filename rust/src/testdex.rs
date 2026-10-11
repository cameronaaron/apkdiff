//! A hand-built, minimal but structurally valid dex, used by the tests.
//!
//! Tests must not depend on the multi-hundred-megabyte APKs that happen to sit
//! next to this repository, and a fixture that is generated rather than
//! recorded can be varied on demand — `sample_variant` produces the same file
//! with one method body altered, which is exactly what a body-hash test needs.

fn uleb(out: &mut Vec<u8>, mut v: u32) {
    loop {
        let mut byte = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            byte |= 0x80;
        }
        out.push(byte);
        if v == 0 {
            break;
        }
    }
}

fn align4(out: &mut Vec<u8>) {
    while !out.len().is_multiple_of(4) {
        out.push(0);
    }
}

fn put_u16(out: &mut [u8], at: usize, v: u16) {
    out[at..at + 2].copy_from_slice(&v.to_le_bytes());
}

fn put_u32(out: &mut [u8], at: usize, v: u32) {
    out[at..at + 4].copy_from_slice(&v.to_le_bytes());
}

/// Strings, in the ascending order a real dex requires.
const STRINGS: [&str; 12] = [
    "I",
    "Lcom/example/Empty;",
    "Lcom/example/Thing;",
    "Ljava/lang/String;",
    "V",
    "abstractly",
    "brokenlist",
    "brokenparams",
    "counter",
    "greet",
    "hello world",
    "run",
];

/// Index of a string by content, so adding one does not require renumbering
/// every constant below.
fn s(name: &str) -> u32 {
    STRINGS
        .iter()
        .position(|x| *x == name)
        .expect("known string") as u32
}

/// A code_item wrapping the given raw instruction bytes, for tests that need
/// to feed the instruction walker something specific.
pub fn code_item_with_insns(insns: &[u8]) -> Vec<u8> {
    let mut c = Vec::new();
    c.extend_from_slice(&1u16.to_le_bytes()); // registers_size
    c.extend_from_slice(&0u16.to_le_bytes()); // ins_size
    c.extend_from_slice(&0u16.to_le_bytes()); // outs_size
    c.extend_from_slice(&0u16.to_le_bytes()); // tries_size
    c.extend_from_slice(&0u32.to_le_bytes()); // debug_info_off
    c.extend_from_slice(&((insns.len() / 2) as u32).to_le_bytes()); // insns_size
    c.extend_from_slice(insns);
    c
}

/// Pool index of the one literal the fixture's code loads. Written as a
/// constant so reordering STRINGS cannot silently point the const-string
/// operand at a different entry.
const HELLO: u8 = 10;

/// The default fixture.
pub fn sample() -> Vec<u8> {
    build(&[0x1a, 0x00, HELLO, 0x00, 0x11, 0x00])
}

/// The same fixture with a different body for `greet`, so a body-hash
/// comparison has something to detect.
pub fn sample_variant() -> Vec<u8> {
    // const-string, then a no-op pair before returning: same signature, new body.
    build(&[0x1a, 0x00, HELLO, 0x00, 0x00, 0x00, 0x11, 0x00])
}

/// A valid dex holding nothing at all. Pooling tests need a second shard to
/// move classes into and out of; this is the cheapest one that parses.
pub fn sample_empty() -> Vec<u8> {
    let mut d = vec![0u8; 112];
    d[0..8].copy_from_slice(b"dex\n035\0");
    d[32..36].copy_from_slice(&112u32.to_le_bytes()); // file_size
    d[36..40].copy_from_slice(&112u32.to_le_bytes()); // header_size
    d[40..44].copy_from_slice(&0x1234_5678u32.to_le_bytes()); // endian_tag
    d // every table size and offset stays zero
}

/// The same fixture with `greet` loading its string through const-string/jumbo
/// rather than const-string, so the wide operand form is exercised.
pub fn sample_jumbo() -> Vec<u8> {
    build(&[0x1b, 0x00, HELLO, 0x00, 0x00, 0x00, 0x11, 0x00])
}

fn build(greet_insns: &[u8]) -> Vec<u8> {
    const HEADER: usize = 112;
    let n_strings = STRINGS.len();
    let n_types = 5;
    let n_protos = 4;
    let n_fields = 1;
    let n_methods = 5;
    let n_classes = 2;

    let string_ids_off = HEADER;
    let type_ids_off = string_ids_off + n_strings * 4;
    let proto_ids_off = type_ids_off + n_types * 4;
    let field_ids_off = proto_ids_off + n_protos * 12;
    let method_ids_off = field_ids_off + n_fields * 8;
    let class_defs_off = method_ids_off + n_methods * 8;
    let data_off = class_defs_off + n_classes * 32;

    let mut d = vec![0u8; data_off];

    // --- data: string_data_item per string
    let mut string_data_offs = Vec::new();
    for s in STRINGS {
        string_data_offs.push(d.len() as u32);
        uleb(&mut d, s.chars().count() as u32);
        d.extend_from_slice(s.as_bytes());
        d.push(0);
    }

    // --- data: type_list for (Ljava/lang/String;I)
    align4(&mut d);
    let type_list_off = d.len() as u32;
    d.extend_from_slice(&2u32.to_le_bytes());
    d.extend_from_slice(&3u16.to_le_bytes()); // Ljava/lang/String;
    d.extend_from_slice(&0u16.to_le_bytes()); // I

    // --- data: code items
    align4(&mut d);
    let code_run_off = d.len() as u32;
    d.extend_from_slice(&code_item_with_insns(&[0x0e, 0x00])); // return-void

    align4(&mut d);
    let code_greet_off = d.len() as u32;
    d.extend_from_slice(&code_item_with_insns(greet_insns));

    // --- data: class_data_item for Lcom/example/Thing;
    let class_data_off = d.len() as u32;
    uleb(&mut d, 0); // static_fields_size
    uleb(&mut d, 2); // instance_fields_size
    uleb(&mut d, 0); // direct_methods_size
    uleb(&mut d, 6); // virtual_methods_size
    uleb(&mut d, 0); // field idx diff -> field 0
    uleb(&mut d, 0x12); // private final
    uleb(&mut d, 9000); // -> a field id that does not exist; must be skipped
    uleb(&mut d, 0x01);
    uleb(&mut d, 0); // method idx diff -> method 0 (run)
    uleb(&mut d, 0x01); // public
    uleb(&mut d, code_run_off);
    uleb(&mut d, 1); // -> method 1 (greet)
    uleb(&mut d, 0x01);
    uleb(&mut d, code_greet_off);
    uleb(&mut d, 1); // -> method 2 (abstractly)
    uleb(&mut d, 0x0401); // public abstract
    uleb(&mut d, 0); // no code
    uleb(&mut d, 1); // -> method 3 (brokenparams): parameters_off past EOF
    uleb(&mut d, 0x01);
    uleb(&mut d, 0);
    uleb(&mut d, 1); // -> method 4 (brokenlist): parameter list overruns EOF
    uleb(&mut d, 0x01);
    uleb(&mut d, 0);
    uleb(&mut d, 9000); // -> a method id that does not exist; must be skipped
    uleb(&mut d, 0x01);
    uleb(&mut d, 0);

    // A parameter list whose declared entry count runs off the end of the
    // file. It has to sit last: anywhere else and the reader would happily
    // parse whatever bytes followed instead of hitting the bounds check.
    align4(&mut d);
    let short_list_off = d.len() as u32;
    d.extend_from_slice(&5u32.to_le_bytes());

    // --- fixed tables
    for (i, off) in string_data_offs.iter().enumerate() {
        put_u32(&mut d, string_ids_off + i * 4, *off);
    }
    for i in 0..n_types {
        put_u32(&mut d, type_ids_off + i * 4, i as u32); // type i -> string i
    }
    // proto 0: ()V
    put_u32(&mut d, proto_ids_off, s("V")); // shorty
    put_u32(&mut d, proto_ids_off + 4, 4); // return type V
    put_u32(&mut d, proto_ids_off + 8, 0); // no parameters
                                           // proto 1: (Ljava/lang/String;I)Ljava/lang/String;
    put_u32(&mut d, proto_ids_off + 12, s("Ljava/lang/String;"));
    put_u32(&mut d, proto_ids_off + 16, 3); // return Ljava/lang/String;
    put_u32(&mut d, proto_ids_off + 20, type_list_off);
    // proto 2: ()V with a parameters_off pointing past the end of the file
    put_u32(&mut d, proto_ids_off + 24, s("V"));
    put_u32(&mut d, proto_ids_off + 28, 4);
    put_u32(&mut d, proto_ids_off + 32, 0x0fff_fff0);
    // proto 3: ()V whose parameter list claims more entries than exist
    put_u32(&mut d, proto_ids_off + 36, s("V"));
    put_u32(&mut d, proto_ids_off + 40, 4);
    put_u32(&mut d, proto_ids_off + 44, short_list_off);
    // field 0: Lcom/example/Thing;.counter:I
    put_u16(&mut d, field_ids_off, 2);
    put_u16(&mut d, field_ids_off + 2, 0);
    put_u32(&mut d, field_ids_off + 4, s("counter"));
    // methods
    for (i, (proto, name)) in [
        (0u16, s("run")),
        (1, s("greet")),
        (0, s("abstractly")),
        (2, s("brokenparams")),
        (3, s("brokenlist")),
    ]
    .iter()
    .enumerate()
    {
        let base = method_ids_off + i * 8;
        put_u16(&mut d, base, 2); // class Lcom/example/Thing;
        put_u16(&mut d, base + 2, *proto);
        put_u32(&mut d, base + 4, *name);
    }
    // class_def 0: Lcom/example/Empty; with no class data
    put_u32(&mut d, class_defs_off, 1);
    put_u32(&mut d, class_defs_off + 24, 0);
    // class_def 1: Lcom/example/Thing;
    put_u32(&mut d, class_defs_off + 32, 2);
    put_u32(&mut d, class_defs_off + 32 + 24, class_data_off);

    // --- header
    d[0..8].copy_from_slice(b"dex\n035\0");
    let file_size = d.len() as u32;
    put_u32(&mut d, 32, file_size); // file_size
    put_u32(&mut d, 36, HEADER as u32); // header_size
    put_u32(&mut d, 40, 0x1234_5678); // endian_tag
    put_u32(&mut d, 56, n_strings as u32);
    put_u32(&mut d, 60, string_ids_off as u32);
    put_u32(&mut d, 64, n_types as u32);
    put_u32(&mut d, 68, type_ids_off as u32);
    put_u32(&mut d, 72, n_protos as u32);
    put_u32(&mut d, 76, proto_ids_off as u32);
    put_u32(&mut d, 80, n_fields as u32);
    put_u32(&mut d, 84, field_ids_off as u32);
    put_u32(&mut d, 88, n_methods as u32);
    put_u32(&mut d, 92, method_ids_off as u32);
    put_u32(&mut d, 96, n_classes as u32);
    put_u32(&mut d, 100, class_defs_off as u32);
    d
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uleb_round_trips_through_the_parser() {
        for v in [0u32, 1, 127, 128, 300, 16_383, 16_384, 1_000_000] {
            let mut buf = Vec::new();
            uleb(&mut buf, v);
            assert_eq!(crate::dex::uleb128(&buf, 0).unwrap().0, v, "value {v}");
        }
    }

    #[test]
    fn align_pads_to_four() {
        for start in 0..4 {
            let mut v = vec![0u8; start];
            align4(&mut v);
            assert_eq!(v.len() % 4, 0);
        }
    }

    #[test]
    fn fixtures_differ_only_in_a_body() {
        assert_ne!(sample(), sample_variant());
        let a = crate::dex::Dex::parse(sample()).unwrap().inventory();
        let b = crate::dex::Dex::parse(sample_variant())
            .unwrap()
            .inventory();
        let sig = |m: &std::collections::BTreeMap<String, crate::dex::Class>| {
            m["Lcom/example/Thing;"]
                .members
                .iter()
                .map(|x| x.line.clone())
                .collect::<Vec<_>>()
        };
        assert_eq!(sig(&a), sig(&b), "signatures must be identical");
        let body = |m: &std::collections::BTreeMap<String, crate::dex::Class>| {
            m["Lcom/example/Thing;"]
                .members
                .iter()
                .find(|x| x.line.contains("greet"))
                .unwrap()
                .body
        };
        assert_ne!(body(&a), body(&b), "bodies must differ");
    }

    #[test]
    fn put_helpers_write_little_endian() {
        let mut b = vec![0u8; 8];
        put_u16(&mut b, 0, 0x1234);
        put_u32(&mut b, 4, 0x8899_aabb);
        assert_eq!(&b[0..2], &[0x34, 0x12]);
        assert_eq!(&b[4..8], &[0xbb, 0xaa, 0x99, 0x88]);
    }

    #[test]
    fn code_item_reports_its_instruction_count() {
        let c = code_item_with_insns(&[0x0e, 0x00, 0x11, 0x00]);
        assert_eq!(u32::from_le_bytes([c[12], c[13], c[14], c[15]]), 2);
    }
}
