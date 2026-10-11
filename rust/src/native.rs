//! Diff the human-readable log strings between two builds of a native library.
//!
//! `.rodata` is one packed blob: the linker stores string literals back to back
//! with no separators, so `strings(1)` returns multi-kilobyte run-on lines and a
//! single inserted byte reflows everything after it. Diffing that output marks
//! nearly every line as changed.
//!
//! Instead: pull out sentence-shaped candidates, then keep only those that do
//! not occur ANYWHERE as raw bytes in the other file. That membership test is
//! immune to reflow, because it never depends on where a literal sits.
//!
//! This is a port of `apkdiff-native-strings.py`, kept byte-for-byte
//! output-compatible with it. The patterns are simple enough to hand-roll, so
//! the crate stays dependency-free and builds in a couple of seconds.

use std::collections::BTreeSet;

/// Bytes that may continue a candidate: `[A-Za-z0-9_'\-.,;:!?()/ ]`.
fn is_cand_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric()
        || matches!(
            b,
            b'_' | b'\''
                | b'-'
                | b'.'
                | b','
                | b';'
                | b':'
                | b'!'
                | b'?'
                | b'('
                | b')'
                | b'/'
                | b' '
        )
}

fn is_printable(b: u8) -> bool {
    (0x20..=0x7e).contains(&b)
}

/// Serde and similar derive macros emit thousands of schema strings whose only
/// difference between builds is the order the linker packed them in. They are
/// never a fix, and they otherwise dominate the output.
fn is_boring(run: &[u8]) -> bool {
    const FIXED: [&[u8]; 6] = [
        b"struct variant",
        b"tuple variant",
        b"internally tagged",
        b"untagged",
        b"variant index",
        b"field index",
    ];
    if FIXED.iter().any(|p| contains(run, p)) {
        return true;
    }
    // `with \d+ element`
    let mut i = 0;
    while let Some(pos) = find_from(run, b"with ", i) {
        let mut j = pos + 5;
        let digits_start = j;
        while j < run.len() && run[j].is_ascii_digit() {
            j += 1;
        }
        if j > digits_start && run[j..].starts_with(b" element") {
            return true;
        }
        i = pos + 1;
    }
    false
}

pub fn contains(hay: &[u8], needle: &[u8]) -> bool {
    find_from(hay, needle, 0).is_some()
}

pub fn find_from(hay: &[u8], needle: &[u8], start: usize) -> Option<usize> {
    if needle.is_empty() || start >= hay.len() || needle.len() > hay.len() - start {
        return None;
    }
    let first = needle[0];
    let last = hay.len() - needle.len();
    let mut i = start;
    // Skip to the next candidate first byte with `position`, which lowers to a
    // vectorised scan; a plain byte-at-a-time loop here was the single hottest
    // thing in the program.
    while i <= last {
        {
            let rel = hay[i..=last].iter().position(|&b| b == first)?;
            let at = i + rel;
            if &hay[at..at + needle.len()] == needle {
                return Some(at);
            }
            i = at + 1;
        }
    }
    None
}

/// Cut a raw candidate down to the part that is plausibly one real message.
fn trim(q: &[u8]) -> Vec<u8> {
    let mut words: Vec<&[u8]> = q.split(|&b| b == b' ').collect();

    // A lowercase letter butting straight into an uppercase one inside the
    // FIRST token ("adGot") means the match began partway through the previous
    // literal, so that token is not part of any message. Acronyms ("JVM;") have
    // no lowercase before the capital and are kept.
    while !words.is_empty() && has_camel_splice(words[0]) {
        words.remove(0);
    }

    let mut out: Vec<&[u8]> = Vec::new();
    for w in words {
        // 18 chars is above almost every English word and below the CamelCase
        // identifier runs that mark a packed-blob boundary.
        if w.len() > 18 {
            // The message's last word is glued to the identifier run that
            // follows it ("...telling the serviceRelativeUrlWithCannotBeABase"),
            // and dropping the whole token drops the word with it. The
            // lowercase-to-uppercase transition is the boundary. Only for a
            // token that STARTS lowercase: one starting with a capital is an
            // ordinary CamelCase word of the message ("init MediaCodec").
            if let Some(edge) = lower_run_before_capital(w) {
                out.push(edge);
            }
            break;
        }
        // Terminal punctuation with more text glued to it ("sample!Packet")
        // is where one literal ends and the next begins; a real sentence puts
        // its ! or ? at the end of the word.
        if let Some(cut) = punct_splice(w) {
            out.push(cut);
            break;
        }
        out.push(w);
    }

    let joined = out.join(&b' ');
    strip(&joined, b" .,;:'")
}

/// `([a-z]{2,})[A-Z]` anchored at the start — the word before the identifier.
fn lower_run_before_capital(w: &[u8]) -> Option<&[u8]> {
    let n = w.iter().take_while(|b| b.is_ascii_lowercase()).count();
    if n >= 2 && w.get(n).is_some_and(|b| b.is_ascii_uppercase()) {
        Some(&w[..n])
    } else {
        None
    }
}

fn has_camel_splice(w: &[u8]) -> bool {
    w.windows(2)
        .any(|p| p[0].is_ascii_lowercase() && p[1].is_ascii_uppercase())
}

/// `([^!?]*[!?])\S` — returns the part up to and including the punctuation.
fn punct_splice(w: &[u8]) -> Option<&[u8]> {
    let p = w.iter().position(|&b| b == b'!' || b == b'?')?;
    if p + 1 < w.len() && w[p + 1] != b' ' {
        Some(&w[..=p])
    } else {
        None
    }
}

fn strip(s: &[u8], chars: &[u8]) -> Vec<u8> {
    let mut a = 0;
    let mut b = s.len();
    while a < b && chars.contains(&s[a]) {
        a += 1;
    }
    while b > a && chars.contains(&s[b - 1]) {
        b -= 1;
    }
    s[a..b].to_vec()
}

/// Real log lines are mostly ordinary lowercase words; packed-blob spans are
/// mostly identifiers. Require a majority of plain-word tokens.
fn wordy(q: &[u8]) -> bool {
    let tokens: Vec<&[u8]> = q.split(|&b| b == b' ').collect();
    let plain = tokens.iter().filter(|w| is_plain_word(w)).count();
    plain >= 2 && plain * 2 >= tokens.len()
}

/// `[a-z][a-z']*[.,;:!?]?` anchored at both ends.
fn is_plain_word(w: &[u8]) -> bool {
    if w.is_empty() || !w[0].is_ascii_lowercase() {
        return false;
    }
    let mut i = 1;
    while i < w.len() && (w[i].is_ascii_lowercase() || w[i] == b'\'') {
        i += 1;
    }
    if i == w.len() {
        return true;
    }
    i + 1 == w.len() && matches!(w[i], b'.' | b',' | b';' | b':' | b'!' | b'?')
}

fn count_spaces(q: &[u8]) -> usize {
    q.iter().filter(|&&b| b == b' ').count()
}

/// Extract every sentence-shaped candidate from a binary.
pub fn phrases(data: &[u8]) -> BTreeSet<Vec<u8>> {
    let mut out = BTreeSet::new();
    let mut pos = 0;
    while pos < data.len() {
        if !is_printable(data[pos]) {
            pos += 1;
            continue;
        }
        let start = pos;
        while pos < data.len() && is_printable(data[pos]) {
            pos += 1;
        }
        let run = &data[start..pos];
        if run.len() < 12 || is_boring(run) {
            continue;
        }
        for cand in candidates(run) {
            let q = trim(cand);
            if count_spaces(&q) >= 2 && q.len() >= 12 && wordy(&q) {
                out.insert(q);
            }
        }
    }
    out
}

/// Non-overlapping greedy matches of `[A-Za-z][cand]{11,88}`, left to right —
/// the same scan order Python's `finditer` uses, so both implementations agree.
fn candidates(run: &[u8]) -> Vec<&[u8]> {
    let mut found = Vec::new();
    let mut i = 0;
    while i < run.len() {
        if !run[i].is_ascii_alphabetic() {
            i += 1;
            continue;
        }
        let mut j = i + 1;
        while j < run.len() && is_cand_byte(run[j]) {
            j += 1;
        }
        let len = j - i;
        if len >= 12 {
            let take = len.min(89);
            found.push(&run[i..i + take]);
            i += take;
        } else {
            i += 1;
        }
    }
    found
}

pub struct Diff {
    pub added: Vec<Vec<u8>>,
    pub removed: Vec<Vec<u8>>,
}

pub fn diff(old: &[u8], new: &[u8]) -> Diff {
    let old_phrases = phrases(old);
    let new_phrases = phrases(new);

    // Substring-searching 32MB is expensive, and nearly every candidate is
    // shared between the two builds. A phrase that was EXTRACTED from the
    // other file is by definition present in its bytes, so the set difference
    // settles those for free and only what survives it needs the raw-byte
    // test. Identical answer, orders of magnitude fewer searches.
    let mut added: Vec<Vec<u8>> = new_phrases
        .iter()
        .filter(|p| !old_phrases.contains(*p) && !contains(old, p))
        .cloned()
        .collect();
    let mut removed: Vec<Vec<u8>> = old_phrases
        .iter()
        .filter(|p| !new_phrases.contains(*p) && !contains(new, p))
        .cloned()
        .collect();

    // A match that began one byte early picks up whatever character preceded
    // the literal, so the same unchanged message can appear as both an addition
    // and a removal differing only in its first character ("lclient is closed"
    // vs "kclient is closed"). Cancel those pairs.
    let add_tails: BTreeSet<Vec<u8>> = added.iter().map(|p| p[1..].to_vec()).collect();
    let rem_tails: BTreeSet<Vec<u8>> = removed.iter().map(|p| p[1..].to_vec()).collect();
    added.retain(|p| !rem_tails.contains(&p[1..]));
    removed.retain(|p| !add_tails.contains(&p[1..]));

    Diff {
        added: repair(&added, new, old),
        removed: repair(&removed, old, new),
    }
}

/// Drop a suffix of a candidate that the other build already contains.
///
/// Two literals packed next to each other with no case change between them
/// ("no JVM; cannot report the failed call" + "failed to spawn thread") look
/// like one sentence, and no rule about shape can tell them apart. What can is
/// that the second half is not new: it sits in the other build verbatim, where
/// the run-on as a whole does not.
///
/// Two conditions, both about what a packed boundary looks like rather than
/// about how long the shared part happens to be. The join has to fall
/// mid-token — the linker concatenates literals with no separator, so
/// "callfailed" is where one ended and the next began, while " call failed" is
/// a sentence carrying on. And the shared part has to be at least as long as
/// the shortest string this tool reports at all: measured on one real pair, a
/// genuine boundary shared 28 characters, a sentence that merely ended
/// similarly shared 12, and two ordinary messages shared 7.
///
/// `None` means the candidate is nothing but a boundary: everything except a
/// fragment too small to have been a candidate in its own right is text the
/// other build already has.
fn cut_shared_tail(cand: &[u8], other: &[u8]) -> Option<Vec<u8>> {
    // The whole length, not "everything but a viable head": capping the search
    // there made the `None` case unreachable, because a tail long enough to
    // leave nothing behind could never be found.
    let (mut lo, mut hi) = (0usize, cand.len());
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        if contains(other, &cand[cand.len() - mid..]) {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    let at = cand.len() - lo;
    if lo < 12 || cand[at] == b' ' || cand[at - 1] == b' ' {
        return Some(cand.to_vec());
    }
    let head = strip(&cand[..at], b" .,;:'");
    if head.len() >= 12 && count_spaces(&head) >= 2 {
        Some(head)
    } else {
        None
    }
}

/// Grow a candidate leftwards while it is still absent from the other build.
///
/// The scan caps a candidate at 89 characters, so a literal packed inside a
/// long printable run arrives with its first word or two missing ("JVM; cannot
/// report the failed call" for a message beginning "no JVM"). The bytes are in
/// front of it; what is missing is a reason to stop. Two of them: only
/// lowercase letters and spaces are crossed, since a capital or a punctuation
/// mark is where the previous literal ended, and the candidate has to stay
/// absent from the other build, which is what made it a finding — tested once,
/// since a longer string cannot be present where a shorter one is not.
///
/// And the walk has to *arrive* at one of those two, or it is thrown away. A
/// walk that runs out of budget inside an unbroken lowercase run never found
/// where the literal starts, and prepending what it crossed would glue the
/// previous message's tail onto this one — inventing a sentence, which is the
/// one failure this tool exists to avoid.
fn extend_left(data: &[u8], cand: &[u8], other: &[u8]) -> Vec<u8> {
    // A candidate the other build already has is one `cut_shared_tail` pared
    // down to an old literal; the re-test in `repair` will drop it, and there
    // is nothing to recover in front of it. Checked once rather than at every
    // step: a superstring cannot be present when the string is not.
    if contains(other, cand) {
        return cand.to_vec();
    }
    // Present by construction: every candidate is a contiguous run of `data`,
    // and both cuts only ever shorten one from an end.
    let at = find_from(data, cand, 0).expect("a candidate is a run of its own file");
    let end = at + cand.len();
    // Bounded, and the bound is a failure rather than a stopping place: no
    // message begins forty characters before where the scan found it. Written
    // as a floor rather than a counter so that running off the front of the
    // file cannot be a separate case — there is nothing before byte zero to
    // walk into, and the same "no boundary found" answer covers it.
    let floor = at.saturating_sub(40);
    let mut start = at;
    let mut arrived = false;
    while start > floor {
        let b = data[start - 1];
        if !(b.is_ascii_lowercase() || b == b' ') {
            arrived = true;
            break;
        }
        start -= 1;
    }
    if !arrived {
        return cand.to_vec();
    }
    // Stopping on a capital says where the previous literal ended — unless a
    // lowercase letter runs straight into that capital, which makes it the
    // start of a CamelCase word and the run just crossed its tail. "adGot
    // empty sample here" walked back over "ot", regluing the very fragment the
    // first-token rule in `trim` exists to drop.
    if start > 1 && data[start - 1].is_ascii_uppercase() && data[start - 2].is_ascii_lowercase() {
        // A candidate carries at least two spaces — `candidates` requires it
        // — so the crossed run and the candidate together always contain one.
        let p = data[start..end]
            .iter()
            .position(|&b| b == b' ')
            .expect("a candidate carries at least two spaces");
        start += p + 1;
    }
    strip(&data[start..end], b" .,;:'")
}

/// Both cuts, then a re-test of what came out.
///
/// The repair only ever applies to a string that was going to be reported
/// anyway, and the result has to pass the same test the original did: present
/// in this build, absent from the other. A repair that produces something both
/// builds contain has found an old literal, and the candidate goes.
fn repair(cands: &[Vec<u8>], mine: &[u8], theirs: &[u8]) -> Vec<Vec<u8>> {
    let mut out: BTreeSet<Vec<u8>> = BTreeSet::new();
    for c in cands {
        if let Some(cut) = cut_shared_tail(c, theirs) {
            let fixed = extend_left(mine, &cut, theirs);
            if !contains(theirs, &fixed) && wordy(&fixed) {
                out.insert(fixed);
            }
        }
    }
    out.into_iter().collect()
}

/// Rendered like Python's `repr()` so the two implementations produce
/// byte-identical reports: prefer single quotes, switch to double quotes when
/// the value itself contains a single quote.
pub fn py_repr(s: &[u8]) -> String {
    let text = String::from_utf8_lossy(s);
    let has_single = text.contains('\'');
    let has_double = text.contains('"');
    let (q, escape_q) = if has_single && !has_double {
        ('"', false)
    } else {
        ('\'', has_single)
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(q);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' if escape_q => out.push_str("\\'"),
            _ => out.push(c),
        }
    }
    out.push(q);
    out
}

/// Sanitised window of surrounding `.rodata`, so a reader can judge whether a
/// hit is a real literal or a fragment spliced out of a packed blob.
pub fn context(data: &[u8], needle: &[u8], span: usize) -> Option<(usize, String)> {
    let i = find_from(data, needle, 0)?;
    let lo = i.saturating_sub(span);
    let hi = (i + span).min(data.len());
    let mut out = String::new();
    let mut in_gap = false;
    for &b in &data[lo..hi] {
        if is_printable(b) {
            out.push(b as char);
            in_gap = false;
        } else if !in_gap {
            out.push_str(" | ");
            in_gap = true;
        }
    }
    Some((i, out))
}

/// Render one half of the report: the strings, each with the surrounding
/// `.rodata` that lets a reader judge whether it is a real literal or a
/// fragment spliced out of a packed blob.
pub fn render(title: &str, rows: &[Vec<u8>], haystack: &[u8]) -> String {
    let sign = if title == "ADDED" { '+' } else { '-' };
    let mut out = format!("=== {title} strings ({}) ===\n\n", rows.len());
    for p in rows {
        out.push_str(&format!("  {sign} {}\n", String::from_utf8_lossy(p)));
        match context(haystack, p, 170) {
            Some((off, ctx)) => out.push_str(&format!("      @{off:#x}  {ctx}\n\n")),
            // Defensive: callers pass the haystack the phrase came from, so a
            // miss should be impossible. Say so rather than panicking.
            None => out.push_str("      (no context)\n\n"),
        }
    }
    out
}

/// Confirm a primary ABI's conclusions hold for another ABI. Every ABI is the
/// same source compiled for a different target, so re-deriving the answer for
/// each is N times the work for the same result; checking membership is a
/// handful of substring searches.
pub fn verify(old: &[u8], new: &[u8], added: &[Vec<u8>], removed: &[Vec<u8>]) -> Vec<String> {
    let mut bad = Vec::new();
    for s in added {
        if !contains(new, s) || contains(old, s) {
            bad.push(format!("  MISMATCH (added): {}", py_repr(s)));
        }
    }
    for s in removed {
        if !contains(old, s) || contains(new, s) {
            bad.push(format!("  MISMATCH (removed): {}", py_repr(s)));
        }
    }
    bad
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &str) -> Vec<u8> {
        s.as_bytes().to_vec()
    }

    #[test]
    fn cand_byte_classes() {
        for b in *b"aZ0_'-.,;:!?()/ " {
            assert!(is_cand_byte(b), "{} should be a candidate byte", b as char);
        }
        for b in [b'"', b'#', b'@', 0x00, 0x7f, b'[', b']'] {
            assert!(!is_cand_byte(b));
        }
    }

    #[test]
    fn printable_bounds() {
        assert!(!is_printable(0x1f));
        assert!(is_printable(0x20));
        assert!(is_printable(0x7e));
        assert!(!is_printable(0x7f));
    }

    #[test]
    fn find_and_contains() {
        assert_eq!(find_from(b"hello world", b"world", 0), Some(6));
        assert_eq!(find_from(b"hello world", b"world", 7), None);
        assert_eq!(find_from(b"aaa", b"", 0), None);
        assert_eq!(find_from(b"aa", b"aaa", 0), None);
        assert_eq!(find_from(b"abcabc", b"abc", 1), Some(3));
        assert_eq!(find_from(b"abc", b"abc", 5), None);
        assert!(contains(b"xyz", b"y"));
        assert!(!contains(b"xyz", b"q"));
    }

    #[test]
    fn boring_detects_serde_schema() {
        assert!(is_boring(b"struct variant Foo::Bar"));
        assert!(is_boring(b"tuple variant"));
        assert!(is_boring(b"internally tagged"));
        assert!(is_boring(b"untagged"));
        assert!(is_boring(b"variant index"));
        assert!(is_boring(b"field index"));
        assert!(is_boring(b"Foo with 3 elements"));
        assert!(!is_boring(b"could not tell the service"));
        // `with` present but not followed by digits+" element"
        assert!(!is_boring(b"with elements and with 3 apples"));
        assert!(!is_boring(b"ends with 12"));
    }

    #[test]
    fn camel_splice_head_is_dropped() {
        assert!(has_camel_splice(b"adGot"));
        assert!(!has_camel_splice(b"JVM;"));
        assert!(!has_camel_splice(b"hello"));
        assert_eq!(trim(b"adGot empty sample here"), v("empty sample here"));
    }

    #[test]
    fn punct_splice_cuts_concatenated_literals() {
        assert_eq!(punct_splice(b"sample!Packet"), Some(&b"sample!"[..]));
        assert_eq!(punct_splice(b"done!"), None);
        assert_eq!(punct_splice(b"plain"), None);
        assert_eq!(punct_splice(b"what?now"), Some(&b"what?"[..]));
        assert_eq!(
            trim(b"got empty sample!Packet too small"),
            v("got empty sample!")
        );
    }

    #[test]
    fn trim_stops_at_overlong_word() {
        let long = "x".repeat(19);
        let input = format!("the quick brown {} tail", long);
        assert_eq!(trim(input.as_bytes()), v("the quick brown"));
    }

    #[test]
    fn trim_strips_edge_punctuation() {
        assert_eq!(
            trim(b"  could not tell the service:  "),
            v("could not tell the service")
        );
    }

    #[test]
    fn plain_word_grammar() {
        assert!(is_plain_word(b"hello"));
        assert!(is_plain_word(b"don't"));
        assert!(is_plain_word(b"end."));
        assert!(is_plain_word(b"a"));
        assert!(!is_plain_word(b""));
        assert!(!is_plain_word(b"Hello"));
        assert!(!is_plain_word(b"end.."));
        assert!(!is_plain_word(b"a1"));
        assert!(!is_plain_word(b"end.x"));
    }

    #[test]
    fn wordy_needs_a_majority_of_words() {
        assert!(wordy(b"could not tell the service"));
        assert!(!wordy(b"Foo Bar Baz"));
        assert!(!wordy(b"one"));
    }

    #[test]
    fn candidates_are_greedy_and_non_overlapping() {
        // Shorter than the 12-byte floor: no match.
        assert!(candidates(b"short one").is_empty());
        let run = b"could not tell the service";
        let c = candidates(run);
        assert_eq!(c.len(), 1);
        assert_eq!(c[0], run);
        // A run longer than 89 is cut at 89 and the scan resumes after it.
        let long = "a".repeat(200);
        let c2 = candidates(long.as_bytes());
        assert_eq!(c2[0].len(), 89);
        assert!(c2.len() > 1);
        // Leading non-alphabetic bytes are skipped.
        let c3 = candidates(b"123 could not tell the service");
        assert_eq!(c3.len(), 1);
    }

    #[test]
    fn phrases_finds_a_message_in_a_binary() {
        let mut blob = vec![0u8; 8];
        blob.extend_from_slice(b"could not tell the service the call failed: ");
        blob.extend_from_slice(&[0u8, 0u8]);
        let p = phrases(&blob);
        assert!(p
            .iter()
            .any(|q| q.starts_with(b"could not tell the service")));
    }

    #[test]
    fn phrases_skips_short_runs_and_boring_runs() {
        assert!(phrases(b"\x00short\x00").is_empty());
        assert!(phrases(b"\x00struct variant Foo with 2 elements here\x00").is_empty());
    }

    #[test]
    fn diff_reports_only_genuinely_new_text() {
        let old = b"\x00the quick brown fox jumps\x00".to_vec();
        let mut new = old.clone();
        new.extend_from_slice(b"could not tell the service it failed\x00");
        let d = diff(&old, &new);
        assert!(d.added.iter().any(|s| s.starts_with(b"could not tell")));
        assert!(d.removed.is_empty());
    }

    #[test]
    fn trim_keeps_the_word_glued_to_an_identifier_run() {
        // Dropping the whole overlong token dropped the message's last word
        // with it: "Call failed; telling the" of a literal reading "service".
        assert_eq!(
            trim(b"Call failed; telling the serviceRelativeUrlWithBase"),
            b"Call failed; telling the service".to_vec()
        );
    }

    #[test]
    fn trim_keeps_a_capitalised_word_whole() {
        // A token STARTING with a capital is an ordinary CamelCase word of the
        // message, not two literals meeting.
        assert_eq!(
            trim(b"failed to init MediaCodecForAudio"),
            b"failed to init MediaCodecForAudio".to_vec()
        );
    }

    #[test]
    fn diff_cuts_a_tail_the_other_build_already_has() {
        let old = b"\x00failed to spawn the worker thread\x00".to_vec();
        let new =
            b"\x00cannot report the failed callfailed to spawn the worker thread\x00".to_vec();
        let d = diff(&old, &new);
        assert_eq!(d.added, vec![b"cannot report the failed call".to_vec()]);
    }

    #[test]
    fn diff_keeps_a_sentence_that_merely_ends_alike() {
        // The join has to fall mid-token, because that is what a packed
        // boundary is; a shared tail starting at a space is a sentence
        // carrying on, and cutting it truncated a real message.
        let old = b"\x00the previous call failed\x00".to_vec();
        let new = b"\x00could not tell the service the call failed\x00".to_vec();
        let d = diff(&old, &new);
        assert_eq!(
            d.added,
            vec![b"could not tell the service the call failed".to_vec()]
        );
    }

    #[test]
    fn diff_drops_a_candidate_that_is_only_a_boundary() {
        // A head too small to have been a candidate in its own right leaves
        // nothing but the join, and a join is not a message.
        let old = b"\x00the queue was drained here\x00".to_vec();
        let new =
            b"\x00xy queuethe queue was drained here\x00a genuinely new message here\x00".to_vec();
        let d = diff(&old, &new);
        assert_eq!(d.added, vec![b"a genuinely new message here".to_vec()]);
    }

    #[test]
    fn diff_does_not_extend_a_head_the_other_build_already_has() {
        // A cut head the other build carries is an old literal: nothing to
        // recover in front of it, and the re-test drops it.
        let old = b"\x00no room left in the queue\x00the queue was drained here\x00".to_vec();
        let new = b"\x00no room left in the queuethe queue was drained here\x00zz another new message\x00"
            .to_vec();
        let d = diff(&old, &new);
        assert_eq!(d.added, vec![b"zz another new message".to_vec()]);
    }

    #[test]
    fn diff_does_not_reglue_the_fragment_trim_dropped() {
        // The walk back crosses lowercase letters, which is exactly what the
        // dropped first token is made of. Stopping on a capital that a
        // lowercase letter runs into keeps "adGot" out.
        let old = b"\x00\x00".to_vec();
        let new = b"\x00adGot empty sample here\x00".to_vec();
        let d = diff(&old, &new);
        assert_eq!(d.added, vec![b"empty sample here".to_vec()]);
    }

    #[test]
    fn diff_will_not_borrow_from_an_unbroken_lowercase_run() {
        // 86 characters of unbroken lowercase, so the 89-character cap
        // swallows the message's first word and leaves no capital to walk back
        // to. Truncated and honest beats completed from the text in front.
        let lead =
            b"the quick brown fox jumps over the lazy dog and keeps on running for a while and then ";
        let old = b"\x00\x00".to_vec();
        let mut new = vec![0u8];
        new.extend_from_slice(lead);
        new.extend_from_slice(b"no room left in the queue for this\x00");
        let d = diff(&old, &new);
        assert!(d
            .added
            .iter()
            .any(|s| s == b"room left in the queue for this"));
        assert!(!d
            .added
            .iter()
            .any(|s| contains(s, b"then") && contains(s, b"room left")));
    }

    #[test]
    fn diff_recovers_a_head_the_scan_cut_off() {
        let old = b"\x00\x00".to_vec();
        let new = b"\x00CallFailed()Vno room left in the queue for this\x00".to_vec();
        let d = diff(&old, &new);
        assert_eq!(
            d.added,
            vec![b"no room left in the queue for this".to_vec()]
        );
    }

    #[test]
    fn diff_cancels_first_character_reflow_pairs() {
        let old = b"\x00kclient is closed for now\x00".to_vec();
        let new = b"\x00lclient is closed for now\x00".to_vec();
        let d = diff(&old, &new);
        assert!(d.added.is_empty(), "added: {:?}", d.added);
        assert!(d.removed.is_empty(), "removed: {:?}", d.removed);
    }

    #[test]
    fn py_repr_matches_python() {
        assert_eq!(py_repr(b"plain text"), "'plain text'");
        assert_eq!(py_repr(b"it didn't work"), "\"it didn't work\"");
        assert_eq!(py_repr(b"say \"hi\""), "'say \"hi\"'");
        assert_eq!(py_repr(b"both ' and \""), "'both \\' and \"'");
        assert_eq!(py_repr(b"back\\slash"), "'back\\\\slash'");
    }

    #[test]
    fn context_sanitises_and_locates() {
        let data = b"\x00\x01hello there world\x00\x02".to_vec();
        let (off, ctx) = context(&data, b"there", 20).unwrap();
        assert_eq!(off, 8);
        assert!(ctx.contains("hello there world"));
        assert!(
            ctx.starts_with(" | "),
            "non-printable bytes collapse to a gap"
        );
        assert!(!ctx.contains('\u{0}'));
        assert!(context(&data, b"absent", 4).is_none());
    }

    #[test]
    fn context_clamps_at_both_ends() {
        let data = b"abcdefgh".to_vec();
        let (off, ctx) = context(&data, b"abc", 1000).unwrap();
        assert_eq!(off, 0);
        assert_eq!(ctx, "abcdefgh");
    }

    #[test]
    fn verify_accepts_consistent_abi() {
        let old = b"alpha beta\x00".to_vec();
        let new = b"alpha beta gamma delta\x00".to_vec();
        let bad = verify(&old, &new, &[v("gamma delta")], &[]);
        assert!(bad.is_empty());
    }

    #[test]
    fn verify_flags_inconsistent_abi() {
        let old = b"alpha beta\x00".to_vec();
        let new = b"alpha beta\x00".to_vec();
        let bad = verify(&old, &new, &[v("gamma")], &[v("alpha")]);
        assert_eq!(bad.len(), 2);
        assert!(bad[0].contains("added"));
        assert!(bad[1].contains("removed"));
    }

    #[test]
    fn render_includes_each_string_with_its_context() {
        let hay = b"\x00hello there world\x00".to_vec();
        let text = render("ADDED", &[v("hello there world")], &hay);
        assert!(text.starts_with("=== ADDED strings (1) ==="));
        assert!(text.contains("  + hello there world"));
        assert!(text.contains("@0x1"));
    }

    #[test]
    fn render_uses_a_minus_sign_for_removals() {
        let hay = b"\x00gone entirely now\x00".to_vec();
        let text = render("REMOVED", &[v("gone entirely now")], &hay);
        assert!(text.contains("  - gone entirely now"));
    }

    #[test]
    fn render_survives_a_string_missing_from_the_haystack() {
        let text = render("ADDED", &[v("absent phrase")], b"unrelated");
        assert!(text.contains("(no context)"));
    }

    #[test]
    fn render_of_nothing_is_just_a_header() {
        assert_eq!(render("ADDED", &[], b""), "=== ADDED strings (0) ===\n\n");
    }

    #[test]
    fn strip_handles_all_stripped() {
        assert_eq!(strip(b"...", b"."), Vec::<u8>::new());
        assert_eq!(strip(b"", b"."), Vec::<u8>::new());
    }

    #[test]
    fn count_spaces_counts() {
        assert_eq!(count_spaces(b"a b c"), 2);
        assert_eq!(count_spaces(b"abc"), 0);
    }

    #[test]
    fn trim_of_all_camel_head_is_empty() {
        assert_eq!(trim(b"aB"), Vec::<u8>::new());
    }
}
