//! Drop-in replacement for `apkdiff-native-strings.py`, same CLI and output.
//!
//!   apkdiff-native-strings OLD.so NEW.so [SPILL_PREFIX]
//!   apkdiff-native-strings --verify OLD.so NEW.so SPILL_PREFIX

use apkdiff::native;
use std::process::ExitCode;

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().collect();
    match run(&argv) {
        Ok(()) => ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("apkdiff-native-strings: {msg}");
            ExitCode::FAILURE
        }
    }
}

fn read(path: &str) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|e| format!("{path}: {e}"))
}

fn run(argv: &[String]) -> Result<(), String> {
    let verify = argv.get(1).map(|s| s == "--verify").unwrap_or(false);
    let args: Vec<&String> = if verify {
        argv[2..].iter().collect()
    } else {
        argv[1..].iter().collect()
    };
    if args.len() < 2 {
        return Err("usage: [--verify] OLD.so NEW.so [SPILL_PREFIX]".into());
    }
    let old = read(args[0])?;
    let new = read(args[1])?;

    if verify {
        let prefix = args.get(2).ok_or("--verify needs a spill prefix")?;
        let added = read_spill(prefix, "added");
        let removed = read_spill(prefix, "removed");
        let bad = native::verify(&old, &new, &added, &removed);
        for line in &bad {
            println!("{line}");
        }
        println!(
            "  verified against primary ABI: {}",
            if bad.is_empty() {
                "OK".to_string()
            } else {
                format!("{} mismatch(es)", bad.len())
            }
        );
        return Ok(());
    }

    let d = native::diff(&old, &new);
    if let Some(prefix) = args.get(2) {
        // One call site, so there is a single I/O failure path to reason about
        // (and to cover) rather than one per file.
        for (kind, rows) in [("added", &d.added), ("removed", &d.removed)] {
            write_spill(prefix, kind, rows)?;
        }
    }

    print!("{}", native::render("ADDED", &d.added, &new));
    print!("{}", native::render("REMOVED", &d.removed, &old));
    Ok(())
}

fn read_spill(prefix: &str, kind: &str) -> Vec<Vec<u8>> {
    std::fs::read(format!("{prefix}.{kind}.txt"))
        .map(|b| {
            b.split(|&c| c == b'\n')
                .filter(|l| !l.is_empty())
                .map(|l| l.to_vec())
                .collect()
        })
        .unwrap_or_default()
}

fn write_spill(prefix: &str, kind: &str, rows: &[Vec<u8>]) -> Result<(), String> {
    let path = format!("{prefix}.{kind}.txt");
    let mut body = Vec::new();
    for r in rows {
        body.extend_from_slice(r);
        body.push(b'\n');
    }
    std::fs::write(&path, body).map_err(|e| format!("{path}: {e}"))
}
