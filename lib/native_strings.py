"""Diff the log strings between two builds of the same native library.

Usage:
  apkdiff-native-strings.py OLD.so NEW.so [SPILL_PREFIX]
      Full analysis: scans both files and reports added/removed strings.

  apkdiff-native-strings.py --verify OLD.so NEW.so SPILL_PREFIX
      Cheap re-check of an earlier full analysis against another ABI. Every ABI
      is the same source compiled for a different target, so re-deriving the
      answer for each is ~4x the work for the same result. Instead, confirm the
      primary ABI's conclusions hold here: each added string present in NEW and
      absent from OLD, each removed string the other way round. That is a
      handful of substring searches rather than a full regex scan.
"""
import re, sys

VERIFY = sys.argv[1] == '--verify'
if VERIFY:
    del sys.argv[1]

old = open(sys.argv[1], 'rb').read()
new = open(sys.argv[2], 'rb').read()

if VERIFY:
    prefix = sys.argv[3]
    bad = 0
    for kind, want_in, want_out in (('added', new, old), ('removed', old, new)):
        try:
            rows = open('%s.%s.txt' % (prefix, kind), 'rb').read().split(b'\n')
        except OSError:
            continue
        for s in filter(None, rows):
            if s not in want_in or s in want_out:
                print('  MISMATCH (%s): %r' % (kind, s))
                bad += 1
    print('  verified against primary ABI: %s'
          % ('OK' if not bad else '%d mismatch(es)' % bad))
    sys.exit(0)

# .rodata is one packed blob: no separators between literals, so `strings`
# yields run-on lines and any insertion reflows everything after it. Pull out
# sentence-shaped candidates instead, then keep only those that do not occur
# ANYWHERE as raw bytes in the other file. That test is immune to reflow.
CAND = re.compile(rb"[A-Za-z][A-Za-z0-9_'\-\.,;:!\?\(\)/ ]{11,88}")

# Serde and similar derive macros emit thousands of schema strings whose only
# "change" between builds is the order the linker packed them in. They are
# never a fix, and they otherwise dominate the output.
BORING = re.compile(rb'struct variant|tuple variant|with \d+ element|'
                    rb'internally tagged|untagged|variant index|field index')

def trim(q):
    # A candidate usually runs off the end of a real message straight into the
    # next literal ("Call failed; telling the serviceRelativeUrlWith...").
    # Cut at the first implausibly long word rather than discarding the hit:
    # 18 chars is above almost every English word and below the CamelCase
    # identifier runs that mark a packed-blob boundary.
    words = q.split(b' ')
    # A lowercase letter butting straight into an uppercase one inside the
    # FIRST token ("adGot") means the match began partway through the previous
    # literal, so that token is not part of any message. Acronyms ("JVM;") have
    # no lowercase before the capital and are kept.
    while words and re.search(rb'[a-z][A-Z]', words[0]):
        words.pop(0)
    out = []
    for w in words:
        if len(w) > 18:
            # The message's last word is glued to the identifier run that
            # follows it ("...telling the serviceRelativeUrlWithCannotBeABase"),
            # and dropping the whole token drops the word with it — the report
            # said "Call failed; telling the" about a literal that reads "...
            # telling the service". The lowercase-to-uppercase transition is
            # the boundary. Only for a token that STARTS lowercase: one
            # starting with a capital is an ordinary CamelCase word of the
            # message ("Failed to init MediaCodec"), not two literals.
            edge = re.match(rb'([a-z]{2,})[A-Z]', w)
            if edge:
                out.append(edge.group(1))
            break
        # Terminal punctuation with more text glued to it ("sample!Packet",
        # "dimensions!recv") is where one literal ends and the next begins —
        # a real sentence puts its ! or ? at the end of the word.
        cut = re.match(rb'([^!?]*[!?])\S', w)
        if cut:
            out.append(cut.group(1))
            break
        out.append(w)
    return b' '.join(out).strip(b" .,;:'")

def wordy(q):
    # Real log lines are mostly ordinary lowercase words. Packed-blob spans
    # are mostly identifiers. Require a majority of plain-word tokens.
    tok = q.split(b' ')
    plain = sum(1 for w in tok if re.fullmatch(rb"[a-z][a-z']*[.,;:!?]?", w))
    return plain >= 2 and plain * 2 >= len(tok)

def phrases(d):
    s = set()
    for run in re.finditer(rb'[ -~]{12,}', d):
        blob = run.group()
        if BORING.search(blob):
            continue
        for m in CAND.finditer(blob):
            q = trim(m.group().strip())
            if q.count(b' ') >= 2 and len(q) >= 12 and wordy(q):
                s.add(q)
    return s

def context(d, needle, span=170):
    i = d.find(needle)
    if i < 0:
        return None, ''
    seg = d[max(0, i - span): i + span]
    return i, re.sub(rb'[^\x20-\x7e]{1,}', b' | ', seg).decode('ascii', 'replace')

old_phrases = phrases(old)
new_phrases = phrases(new)

# Substring-searching 32MB is expensive, and nearly every candidate is shared
# between the two builds. A phrase EXTRACTED from the other file is by
# definition present in its bytes, so the set difference settles those for free
# and only what survives it needs the raw-byte test. Identical answer, orders
# of magnitude fewer searches.
added   = sorted(p for p in new_phrases if p not in old_phrases and p not in old)
removed = sorted(p for p in old_phrases if p not in new_phrases and p not in new)

# A match that began one byte early picks up whatever character preceded the
# literal, so the same unchanged message can appear as both an addition and a
# removal differing only in its first character ("lclient is closed" vs
# "kclient is closed"). Cancel those pairs.
_tails = {p[1:] for p in removed}
_addtails = {p[1:] for p in added}
added   = [p for p in added   if p[1:] not in _tails]
removed = [p for p in removed if p[1:] not in _addtails]

def cut_shared_tail(cand, other):
    """Drop a suffix of a candidate that the other build already contains.

    Two literals packed next to each other with no case change between them
    ("no JVM; cannot report the failed call" + "failed to spawn thread") look
    like one sentence, and no rule about shape can tell them apart. What can is
    that the second half is not new: it sits in the other build verbatim, where
    the run-on as a whole does not. Cutting the longest such suffix leaves the
    literal that actually arrived.

    Two conditions, and both are about what a packed boundary looks like
    rather than about how long the shared part happens to be. The join has to
    fall mid-token — the linker concatenates literals with no separator, so
    "callfailed" is where one ended and the next began, while " call failed"
    is a sentence carrying on and shares its tail with the other build for the
    ordinary reason that both builds say similar things. And the shared part
    has to be at least as long as the shortest string this tool will report at
    all, or it is a coincidence: measured on one real pair, a genuine boundary
    shared 28 characters, a sentence that merely ended similarly shared 12, and
    two ordinary messages shared 7.
    """
    # The whole length, not "everything but a viable head": capping the search
    # there made the case this returns None for unreachable, because a tail
    # long enough to leave nothing behind could never be found.
    lo, hi = 0, len(cand)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if other.find(cand[-mid:]) >= 0:
            lo = mid
        else:
            hi = mid - 1
    if lo < 12 or b' ' in (cand[-lo:-lo + 1], cand[-lo - 1:-lo]):
        return cand
    head = cand[:-lo].rstrip(b" .,;:'")
    # A head too small to have been a candidate in its own right means the
    # whole of this match except a fragment is text the other build already
    # has: what is new about it is the boundary, and a boundary is not a
    # message. `rofilesset_profile returned no profile` was two old literals
    # meeting, reported as one new one.
    return head if len(head) >= 12 and head.count(b' ') >= 2 else None


def extend_left(data, cand, other):
    """Grow a candidate leftwards while it is still absent from the other build.

    The scan caps a candidate at 89 characters, so a literal packed inside a
    long printable run arrives with its first word or two missing ("JVM; cannot
    report the failed call" for a message that begins "no JVM"). The bytes are
    right there in front of it; what is missing is a reason to stop.

    Two of them. Only lowercase letters and spaces are crossed — a capital or a
    punctuation mark is where the previous literal ended — and every step has
    to keep the candidate absent from the other build, which is what made it a
    finding in the first place — a condition that only has to be tested once,
    since a longer string cannot be present where a shorter one is not.

    And the walk has to *arrive* at one of those two, or it is thrown away. A
    walk that runs out of budget in the middle of an unbroken lowercase run
    never found where the literal starts, and prepending whatever it crossed
    would glue the tail of the previous message onto this one — inventing a
    sentence, which is the one failure this tool exists to avoid. A missing
    first word is a truncation the reader can see; a borrowed one is not.
    """
    # A candidate the other build already has is one `cut_shared_tail` pared
    # down to an old literal; the re-test below will drop it, and there is
    # nothing to recover in front of it. Checked once here rather than at every
    # step: a superstring cannot be present when the string is not, so the
    # in-loop form could never fire after this one passed.
    if other.find(cand) >= 0:
        return cand
    # Present by construction: every candidate is a contiguous run of `data`,
    # and both cuts above only ever shorten one from an end.
    at = data.find(cand)
    end, start = at + len(cand), at
    # Bounded, and the bound is a failure rather than a stopping place: no
    # message begins forty characters before where the scan found it. Written
    # as a floor rather than a counter so that running off the front of the
    # file cannot be a separate case — there is nothing before byte zero to
    # walk into, and the same "no boundary found" answer covers it.
    floor, arrived = max(at - 40, 0), False
    while start > floor:
        if not (0x61 <= data[start - 1] <= 0x7A or data[start - 1] == 0x20):
            arrived = True
            break
        start -= 1
    if not arrived:
        return cand
    # Stopping on a capital says where the previous literal ended — unless a
    # lowercase letter runs straight into that capital, which makes it the
    # start of a CamelCase word and the run just crossed its tail. "adGot empty
    # sample here" walked back over "ot", regluing the very fragment the first
    # token rule above exists to drop.
    if (start > 1 and 0x41 <= data[start - 1] <= 0x5A
            and 0x61 <= data[start - 2] <= 0x7A):
        # A candidate carries at least two spaces — `phrases` requires it — so
        # the crossed run and the candidate together always contain one.
        start = data.index(b' ', start, end) + 1
    return data[start:end].strip(b" .,;:'")


def repair(cands, mine, theirs):
    """Both cuts, then a re-test of what came out.

    The repair is only ever applied to a string that was going to be reported
    anyway, and the result has to pass the same test the original did: present
    in this build, absent from the other. A repair that produces something both
    builds contain has found an old literal, and the candidate goes.
    """
    out = []
    for c in cands:
        fixed = cut_shared_tail(c, theirs)
        if fixed is None:
            continue
        fixed = extend_left(mine, fixed, theirs)
        if theirs.find(fixed) < 0 and wordy(fixed):
            out.append(fixed)
    return sorted(set(out))


added = repair(added, new, old)
removed = repair(removed, old, new)

# Machine-readable spill for the findings stage.
if len(sys.argv) > 3:
    for name, rows in (('added', added), ('removed', removed)):
        with open('%s.%s.txt' % (sys.argv[3], name), 'wb') as fh:
            fh.write(b'\n'.join(rows) + b'\n')

print("=== ADDED strings (%d) ===\n" % len(added))
for p in added:
    off, ctx = context(new, p)
    print("  + %s" % p.decode('ascii', 'replace'))
    print("      @0x%x  %s\n" % (off, ctx))
print("=== REMOVED strings (%d) ===\n" % len(removed))
for p in removed:
    off, ctx = context(old, p)
    print("  - %s" % p.decode('ascii', 'replace'))
    print("      @0x%x  %s\n" % (off, ctx))
