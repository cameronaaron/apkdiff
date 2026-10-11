//! Compare dex files directly, without disassembling them.
//!
//!   apkdiff-dex OLD.dex NEW.dex OUT_PREFIX
//!   apkdiff-dex --pool OUT_PREFIX --old A.dex [B.dex...] --new C.dex [D.dex...]
//!
//! The --pool form merges each side's dex files before comparing, so a class
//! that R8 moved between shards is not mistaken for one added and one removed.
//!
//! Writes, alongside a human-readable report on stdout:
//!   OUT_PREFIX.norm     STATUS<TAB>class
//!   OUT_PREFIX.api      class<TAB>+|-<TAB>smali signature
//!   OUT_PREFIX.rank     magnitude<TAB>class, biggest first
//!   OUT_PREFIX.owner    new literal<TAB>class whose code loads it
//!   OUT_PREFIX.str.old  quoted string literals
//!   OUT_PREFIX.str.new  quoted string literals

use apkdiff::{dex::Dex, dexdiff};
use std::process::ExitCode;

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().collect();
    match run(&argv) {
        Ok(()) => ExitCode::SUCCESS,
        Err(msg) => {
            eprintln!("apkdiff-dex: {msg}");
            ExitCode::FAILURE
        }
    }
}

fn run(argv: &[String]) -> Result<(), String> {
    // Test-suite hook: write a small, valid dex so the shell tests can build
    // fixture APKs without shipping a binary blob or depending on a real app.
    if argv.get(1).map(|s| s == "--emit-fixture").unwrap_or(false) {
        let path = argv
            .get(2)
            .ok_or("usage: --emit-fixture PATH [--variant|--empty]")?;
        let bytes = match argv.get(3).map(|s| s.as_str()) {
            Some("--variant") => apkdiff::testdex::sample_variant(),
            Some("--empty") => apkdiff::testdex::sample_empty(),
            _ => apkdiff::testdex::sample(),
        };
        return std::fs::write(path, bytes).map_err(|e| format!("{path}: {e}"));
    }
    let (prefix, old_paths, new_paths) = parse(argv)?;

    // Decoding a dex's string pool is tens of megabytes of allocation per file
    // and no file's decode depends on another's, so read them all at once. The
    // results stay in argument order, and the first error in that order is the
    // one reported, exactly as the sequential loop reported it.
    let loaded: Vec<Result<Dex, String>> = std::thread::scope(|scope| {
        let workers: Vec<_> = old_paths
            .iter()
            .chain(new_paths.iter())
            .map(|p| scope.spawn(|| load(p)))
            .collect();
        workers
            .into_iter()
            .map(|w| w.join().expect("a dex loading thread panicked"))
            .collect()
    });
    let mut dexes = Vec::with_capacity(loaded.len());
    for one in loaded {
        dexes.push(one?);
    }
    let new: Vec<Dex> = dexes.split_off(old_paths.len());
    let old: Vec<Dex> = dexes;

    // The two sides share nothing until `compare`, and each side's own shards
    // are read in parallel inside `pool`, so reading them one side after the
    // other leaves the tail of the larger side running alone.
    let ((old_inv, old_lits), (new_inv, new_lits)) = std::thread::scope(|scope| {
        let older = scope.spawn(|| dexdiff::pool(&old));
        let newer = dexdiff::pool(&new);
        (older.join().expect("a dex pooling thread panicked"), newer)
    });
    let deltas = dexdiff::compare(&old_inv, &new_inv);

    // One call site for the same reason: a single I/O failure path.
    for (suffix, lines) in [
        ("norm", dexdiff::norm_lines(&deltas)),
        ("api", dexdiff::api_lines(&deltas)),
        ("rank", dexdiff::rank_lines(&deltas)),
        ("owner", dexdiff::owner_lines(&old_inv, &new_inv)),
        ("str.old", old_lits),
        ("str.new", new_lits),
    ] {
        write(prefix, suffix, &lines)?;
    }
    if old.len() > 1 || new.len() > 1 {
        println!(
            "  pooled {} old and {} new dex file(s)",
            old.len(),
            new.len()
        );
    }

    let changed = deltas
        .iter()
        .filter(|d| d.status == dexdiff::Status::Changed)
        .count();
    let new_c = deltas
        .iter()
        .filter(|d| d.status == dexdiff::Status::New)
        .count();
    let gone = deltas
        .iter()
        .filter(|d| d.status == dexdiff::Status::Gone)
        .count();
    println!("  classes: {changed} changed, {new_c} new, {gone} removed");
    println!(
        "  members: {} added, {} removed",
        deltas.iter().map(|d| d.added.len()).sum::<usize>(),
        deltas.iter().map(|d| d.removed.len()).sum::<usize>()
    );

    // Every artefact is on disk and every line is printed. What remains is
    // freeing a map of a quarter of a million Strings one at a time so that
    // the process can exit — which a profile of a real pair showed as a
    // quarter of its samples, all of them inside the system allocator's free.
    // Exiting here skips that: 279ms to 239ms on a four-shard read.
    //
    // Nothing here has a destructor whose effect would be lost. Every file was
    // written by fs::write, which closes as it returns, and stdout is a
    // LineWriter that flushes on the newline every line above ends with — so
    // there is no buffer left to empty and no flush to check. An explicit
    // flush was tried first and dropped again: it changed no output under a
    // pipe, and its error arm could not be reached by any test, which is a
    // worse thing to leave in the file than to leave out. The shell suite
    // pins the summary and the artefacts' last lines so that an exit which
    // ever did start truncating would fail the build rather than the report.
    std::process::exit(0);
}

/// Both call shapes resolve to (prefix, old paths, new paths).
fn parse(argv: &[String]) -> Result<(&String, Vec<&String>, Vec<&String>), String> {
    const USAGE: &str = "usage: OLD.dex NEW.dex OUT_PREFIX\n   \
                         or: --pool OUT_PREFIX --old A.dex... --new B.dex...";
    if argv.get(1).map(|s| s == "--pool") != Some(true) {
        if argv.len() < 4 {
            return Err(USAGE.into());
        }
        return Ok((&argv[3], vec![&argv[1]], vec![&argv[2]]));
    }
    let prefix = argv.get(2).ok_or(USAGE)?;
    let (mut old, mut new, mut side) = (Vec::new(), Vec::new(), ' ');
    for a in &argv[3..] {
        match a.as_str() {
            "--old" => side = 'o',
            "--new" => side = 'n',
            _ => match side {
                'o' => old.push(a),
                'n' => new.push(a),
                _ => return Err(format!("{a} came before --old or --new")),
            },
        }
    }
    if old.is_empty() || new.is_empty() {
        return Err("--pool needs at least one dex on each side".into());
    }
    Ok((prefix, old, new))
}

fn load(path: &str) -> Result<Dex, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
    Dex::parse(bytes).map_err(|e| format!("{path}: {e}"))
}

fn write(prefix: &str, suffix: &str, lines: &[String]) -> Result<(), String> {
    let path = format!("{prefix}.{suffix}");
    let mut body = lines.join("\n");
    if !body.is_empty() {
        body.push('\n');
    }
    std::fs::write(&path, body).map_err(|e| format!("{path}: {e}"))
}
