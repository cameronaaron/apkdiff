#!/usr/bin/env bash
#
# Functional tests for apkdiff.sh.
#
# Every case builds its own miniature APK or AAB, so the suite runs in seconds
# and does not depend on the large real-world archives that happen to sit in
# this directory. Run under bashcov for line coverage:
#
#   bashcov -- tests/test_apkdiff_sh.sh
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APKDIFF="$ROOT/apkdiff.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; }
check() {                            # description haystack needle
  case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "expected to find: $3" ;; esac
}
check_not() {
  case "$2" in *"$3"*) bad "$1" "did not expect: $3" ;; *) ok "$1" ;; esac
}

# ---------------------------------------------------------------- fixtures
#
# A binary AndroidManifest.xml cannot be synthesised by hand, so the fixtures
# below carry a placeholder: stage 2 reports that it could not decode it, which
# is itself a path worth exercising. The dex reader is covered by the Rust and
# Python suites against generated dex files; what these tests cover is the
# shell around them.

mkso() {                             # path text...   (each text becomes a
  local path="$1"; shift             #  NUL-terminated literal, as a linker
  printf '\177ELF\000\000\000\000' > "$path"   #  would pack them)
  local t
  for t in "$@"; do
    printf '%s' "$t" >> "$path"
    printf '\000' >> "$path"
  done
  head -c 512 /dev/zero >> "$path"
}

mkapk() {                            # dir -> zip at $dir.apk
  (cd "$1" && zip -qr "$1.apk" .)
}

MKAXML="$ROOT/tests/mkaxml.py"
mkmanifest() {                       # dir versionCode [package]
  python3 "$MKAXML" "$1/AndroidManifest.xml" "$2" "${3:-com.example.app}"
}

DEXBIN="$ROOT/rust/target/release/apkdiff-dex"
mkdex() {                            # path [--variant]
  "$DEXBIN" --emit-fixture "$@"
}

# ---------------------------------------------------------------- argument handling

echo "argument handling"

out=$("$APKDIFF" 2>&1); check "no arguments prints usage" "$out" "usage:"
out=$("$APKDIFF" /nope/a.apk /nope/b.apk 2>&1); check "missing file is reported" "$out" "no such file"
out=$("$APKDIFF" --bogus 2>&1); check "unknown flag is rejected" "$out" "unknown flag"
out=$("$APKDIFF" --help 2>&1); check "help describes the tool" "$out" "find what actually changed"
out=$("$APKDIFF" --help 2>&1); check "help lists the stages" "$out" "7. findings"
# Help is the header comment; a fixed line range once printed code after it.
check_not "help stops at the end of the header" "$out" "pipefail"
check_not "help leaves out the maintainer gotchas" "$out" "Gotchas"

touch "$TMP/only.apk"
out=$("$APKDIFF" "$TMP/only.apk" 2>&1)
check "one unreadable argument is reported" "$out" "cannot identify"

# ---------------------------------------------------------------- inventory

echo
echo "inventory"

mkdir -p "$TMP/a" "$TMP/b"
echo "shared" > "$TMP/a/same.txt"; echo "shared" > "$TMP/b/same.txt"
echo "one"    > "$TMP/a/changed.txt"; echo "two" > "$TMP/b/changed.txt"
echo "gone"   > "$TMP/a/removed.txt"
echo "fresh"  > "$TMP/b/added.txt"
mkapk "$TMP/a"; mkapk "$TMP/b"

out=$("$APKDIFF" "$TMP/a.apk" "$TMP/b.apk" -o "$TMP/inv" 2>&1)
rc=$?
[ $rc -eq 0 ] && ok "runs to completion on a trivial pair" || bad "runs to completion on a trivial pair" "exit $rc"
report="$TMP/inv/report/1-inventory.txt"
check "counts the changed entry"  "$(cat "$report")" "changed.txt"
check "counts the added entry"    "$(cat "$report")" "added.txt"
check "counts the removed entry"  "$(cat "$report")" "removed.txt"
check_not "ignores the identical entry" "$(grep -A20 '^--- changed' "$report")" "same.txt"
check "writes a findings report" "$(ls "$TMP/inv/report")" "7-findings.txt"

# With no -o, a run goes under runs/ in the working directory rather than
# dropping a sibling directory next to whatever else is there.
(cd "$TMP" && "$APKDIFF" "$TMP/a.apk" "$TMP/b.apk" >/dev/null 2>&1)
if [ -f "$TMP/runs/a-vs-b/report/1-inventory.txt" ]; then
  ok "defaults to runs/OLD-vs-NEW"
else
  bad "defaults to runs/OLD-vs-NEW" "no runs/a-vs-b under $TMP"
fi

# Re-running into the same directory must reuse it rather than fail.
out=$("$APKDIFF" "$TMP/a.apk" "$TMP/b.apk" -o "$TMP/inv" 2>&1)
[ $? -eq 0 ] && ok "re-runs into an existing output directory" || bad "re-runs into an existing output directory"

out=$("$APKDIFF" "$TMP/a.apk" "$TMP/b.apk" -o "$TMP/inv" --clean 2>&1)
[ $? -eq 0 ] && ok "--clean starts from scratch" || bad "--clean starts from scratch"

# Identical inputs: nothing at all should be reported.
out=$("$APKDIFF" "$TMP/a.apk" "$TMP/a.apk" -o "$TMP/same" 2>&1)
check "identical archives report no changes" "$(cat "$TMP/same/report/1-inventory.txt")" "changed=0"
check "and no findings"                      "$(cat "$TMP/same/report/7-findings.txt")" "No heuristic findings"

# ---------------------------------------------------------------- native stage

echo
echo "native libraries"

mkdir -p "$TMP/n1/lib/arm64-v8a" "$TMP/n2/lib/arm64-v8a"
mkso "$TMP/n1/lib/arm64-v8a/libx.so" "the quick brown fox jumps over"
mkso "$TMP/n2/lib/arm64-v8a/libx.so" "the quick brown fox jumps over" "could not reach the remote service"
mkapk "$TMP/n1"; mkapk "$TMP/n2"

out=$("$APKDIFF" "$TMP/n1.apk" "$TMP/n2.apk" -o "$TMP/nat" 2>&1)
check "native stage runs"            "$out" "stage 4"
check "reports the new log string"   "$(cat "$TMP/nat/report/4-native.txt")" "could not reach the remote service"
check "findings call it error handling" "$(cat "$TMP/nat/report/7-findings.txt")" "NEW ERROR HANDLING"

# The fixture .so carries an ELF magic and nothing else, so nm parses no
# dynamic symbol table from either side. Two empty symbol lists are the absence
# of a result, not a result: reporting them as "identical exports" would be a
# negative the tool never established, and the same shape appears for real when
# nm is a BSD build that does not take -D/--defined-only.
if command -v nm >/dev/null; then
  natrep="$(cat "$TMP/nat/report/4-native.txt")"
  check "an unreadable symbol table is admitted" "$natrep" "nm read no dynamic symbols"
  check_not "and is not called identical"        "$natrep" "(identical"
fi

# A second ABI is verified against the first rather than re-analysed.
mkdir -p "$TMP/m1/lib/arm64-v8a" "$TMP/m1/lib/x86_64" "$TMP/m2/lib/arm64-v8a" "$TMP/m2/lib/x86_64"
for abi in arm64-v8a x86_64; do
  mkso "$TMP/m1/lib/$abi/libx.so" "the quick brown fox jumps over"
  mkso "$TMP/m2/lib/$abi/libx.so" "the quick brown fox jumps over" "could not reach the remote service"
done
mkapk "$TMP/m1"; mkapk "$TMP/m2"
out=$("$APKDIFF" "$TMP/m1.apk" "$TMP/m2.apk" -o "$TMP/multi" 2>&1)
check "secondary ABI is verified, not rescanned" "$out" "verifying"
check "verification passes"                      "$(cat "$TMP/multi/report/4-native.txt")" "verified against primary ABI: OK"

# A library is fetched in the background from stage 1 on, so every other stage
# is looking at "$W/<tag>/<entry>" while unzip is still inflating into it. The
# promise `extract_one` makes is that the entry appears at that path whole or
# not at all. Watch the path for the whole run and record any size that is not
# the final one: with unzip writing straight to the destination, a 32MB library
# is observed part-written almost every time. The check cannot fail for the
# wrong reason — a run too fast to catch mid-flight records nothing.
mkdir -p "$TMP/big1/lib/arm64-v8a" "$TMP/big2/lib/arm64-v8a"
mkso "$TMP/big1/lib/arm64-v8a/libx.so" "the quick brown fox jumps over"
mkso "$TMP/big2/lib/arm64-v8a/libx.so" "the quick brown fox jumps over" "could not reach the remote service"
# Big enough to take a measurable time to inflate; zeros compress away, so the
# fixture archive itself stays tiny.
head -c 33554432 /dev/zero >> "$TMP/big1/lib/arm64-v8a/libx.so"
head -c 33554432 /dev/zero >> "$TMP/big2/lib/arm64-v8a/libx.so"
mkmanifest "$TMP/big1" 1; mkmanifest "$TMP/big2" 2
mkapk "$TMP/big1"; mkapk "$TMP/big2"
bigdst="$TMP/big/work/new/lib/arm64-v8a/libx.so"
bigsize=$(wc -c < "$TMP/big2/lib/arm64-v8a/libx.so" | tr -d ' ')
"$APKDIFF" "$TMP/big1.apk" "$TMP/big2.apk" -o "$TMP/big" >/dev/null 2>&1 &
bigpid=$!
bigpartial=""
while kill -0 "$bigpid" 2>/dev/null; do
  [ -f "$bigdst" ] || continue
  bigseen=$(wc -c < "$bigdst" 2>/dev/null | tr -d ' ')
  [ "${bigseen:-$bigsize}" = "$bigsize" ] || bigpartial="$bigseen"
done
wait "$bigpid"
[ -z "$bigpartial" ] \
  && ok "a library is never visible half-extracted" \
  || bad "a library is never visible half-extracted" "saw $bigpartial of $bigsize bytes"
check "the background pass still finds the new string" \
  "$(cat "$TMP/big/report/4-native.txt")" "could not reach the remote service"
# Staging is an implementation detail of the fetch, not an artefact of the run.
if [ -n "$(find "$TMP/big/work" -maxdepth 1 -name '.staging.*' -print -quit)" ]; then
  bad "staging directories are cleaned up"
else
  ok "staging directories are cleaned up"
fi

# ---------------------------------------------------------------- resource XML
#
# res/xml/ is the same binary XML the manifest is. Compared by CRC alone, a
# FileProvider root moving out of the app's own files/ directory reached the
# report as the words "1 non-code file changed".

echo
echo "resource XML"

mkdir -p "$TMP/x1/res/xml" "$TMP/x2/res/xml"
python3 "$MKAXML" --paths "$TMP/x1/res/xml/file_paths.xml" "attachments/"
python3 "$MKAXML" --paths "$TMP/x2/res/xml/file_paths.xml" "../app_flutter/attachments/"
mkapk "$TMP/x1"; mkapk "$TMP/x2"

out=$("$APKDIFF" "$TMP/x1.apk" "$TMP/x2.apk" -o "$TMP/rx" 2>&1)
check "resource XML is decoded"    "$out" "resource XML: 1 changed"
rxrep="$(cat "$TMP/rx/report/2-resource-xml.txt")"
check "its own report names the file" "$rxrep" "res/xml/file_paths.xml"
check "and shows the old value"       "$rxrep" "-/paths/files-path  path=attachments/"
check "and the new one"               "$rxrep" "+/paths/files-path  path=../app_flutter/attachments/"
rxfind="$(cat "$TMP/rx/report/7-findings.txt")"
check "findings raise it"          "$rxfind" "[RESOURCE XML CHANGE]"
check "with the value that changed" "$rxfind" "../app_flutter/attachments/"
# OTHER FILES names what nothing looked at; stage 2 looked at this one.
check_not "and do not name it twice" "$rxfind" "[OTHER FILES]"
# It must stay out of the manifest diff, every line of which stage 7 reports
# as a manifest change.
check_not "manifest report is untouched" \
  "$(cat "$TMP/rx/report/2-manifest.txt")" "files-path"

# The same document with a UTF-8 string pool instead of a UTF-16 one: aapt2
# picks between the two, so a CRC can move with nothing in the file changing.
# That is a negative result and gets stated, not dropped on the floor.
mkdir -p "$TMP/z1/res/xml" "$TMP/z2/res/xml"
python3 "$MKAXML" --paths "$TMP/z1/res/xml/file_paths.xml" "attachments/"
python3 "$MKAXML" --paths "$TMP/z2/res/xml/file_paths.xml" "attachments/" --utf8
mkapk "$TMP/z1"; mkapk "$TMP/z2"
out=$("$APKDIFF" "$TMP/z1.apk" "$TMP/z2.apk" -o "$TMP/rz" 2>&1)
check "a re-encode is told from a change" "$out" "resource XML: 0 changed, 1 re-encoded only"
check "the report says so"  "$(cat "$TMP/rz/report/2-resource-xml.txt")" "re-encoded; no content change"
check "and findings say so" "$(cat "$TMP/rz/report/7-findings.txt")" "[RE-ENCODED XML]"

# An XML entry that is not decodable is said to be, not passed over.
mkdir -p "$TMP/y1/res/xml" "$TMP/y2/res/xml"
printf 'one' > "$TMP/y1/res/xml/opaque.xml"
printf 'two' > "$TMP/y2/res/xml/opaque.xml"
mkapk "$TMP/y1"; mkapk "$TMP/y2"
out=$("$APKDIFF" "$TMP/y1.apk" "$TMP/y2.apk" -o "$TMP/ry" 2>&1)
check "an undecodable entry is counted" "$out" "1 undecodable"
check "and reaches findings"            "$(cat "$TMP/ry/report/7-findings.txt")" "[UNDECODED XML]"

# A bundle keeps its resource XML as protobuf, so every entry would come back
# undecodable — 921 of them on a real pair, at 84s. MIXED PACKAGING already
# says it in one line; this stage stands down rather than repeating it.
mkdir -p "$TMP/bx/res/xml"
cp "$TMP/x2/res/xml/file_paths.xml" "$TMP/bx/res/xml/"
printf 'x' > "$TMP/bx/BundleConfig.pb"
mkapk "$TMP/bx"
out=$("$APKDIFF" "$TMP/x1.apk" "$TMP/bx.apk" -o "$TMP/rb" 2>&1)
check "a bundle stands the stage down" "$out" "skipped; a bundle stores it as protobuf"
check_not "and raises no XML findings" \
  "$(cat "$TMP/rb/report/7-findings.txt")" "UNDECODED XML"

# ---------------------------------------------------------------- resources
#
# resources.arsc is the one file in the archive still written in the words the
# developer chose. Compared by CRC alone it reaches the report as "1 non-code
# file changed", which says nothing about the string that moved inside it.

echo
echo "resources"

MKARSC="$ROOT/tests/mkarsc.py"
mkdir -p "$TMP/res1" "$TMP/res2" "$TMP/res3"
python3 "$MKARSC" "$TMP/res1/resources.arsc"
python3 "$MKARSC" "$TMP/res2/resources.arsc" --sparse
printf 'not a table' > "$TMP/res3/resources.arsc"
mkapk "$TMP/res1"; mkapk "$TMP/res2"; mkapk "$TMP/res3"

# Two encodings of the same values: the report has to say the table moved and
# no value did, because the CRC differing is the only reason it was read.
out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/res2.apk" -o "$TMP/rs" 2>&1)
check "the table is read"          "$out" "resources: 0 value(s) differ"
check "and the report says so"     "$(cat "$TMP/rs/report/2-resources.txt")" \
  "(the resource values are identical)"
check "and findings state the negative" "$(cat "$TMP/rs/report/7-findings.txt")" \
  "[RESOURCES UNCHANGED]"
check_not "without calling it an unread file" \
  "$(cat "$TMP/rs/report/7-findings.txt")" "resources.arsc"

# A value that actually moved is quoted, with the name its author gave it.
python3 - "$TMP/res4" "$ROOT/tests" <<'EOF'
import sys, os
sys.path.insert(0, sys.argv[2])
import mkarsc
os.makedirs(sys.argv[1], exist_ok=True)
mkarsc.VALUES[1] = "Goodbye, world"
open(os.path.join(sys.argv[1], "resources.arsc"), "wb").write(mkarsc.sample())
EOF
mkapk "$TMP/res4"
out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/res4.apk" -o "$TMP/rv" 2>&1)
check "a changed value is counted" "$out" "resources: 1 value(s) differ"
check "the report quotes both sides" "$(cat "$TMP/rv/report/2-resources.txt")" \
  "was  Hello, world"
resfind="$(cat "$TMP/rv/report/7-findings.txt")"
check "findings name the resource"  "$resfind" "[REWORDED RESOURCE]"
check "with the developer's own name for it" "$resfind" "string/greeting"

# A table this reader cannot parse leaves both sides out rather than reporting
# every value in the build as new.
out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/res3.apk" -o "$TMP/rw" 2>&1)
check "an unreadable table is reported" "$out" "could not read one of the resource tables"
check_not "and no value is claimed"     "$(cat "$TMP/rw/report/7-findings.txt")" \
  "RESOURCE"

# A bundle stores the table as protobuf, which this reader cannot parse.
mkdir -p "$TMP/resb"
cp "$TMP/res2/resources.arsc" "$TMP/resb/"
printf 'x' > "$TMP/resb/BundleConfig.pb"
mkapk "$TMP/resb"
out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/resb.apk" -o "$TMP/rt" 2>&1)
check "a bundle stands the stage down" "$out" "a bundle stores the table as protobuf"

# ---------------------------------------------------------------- notes
#
# Stage 8 re-files stage 7's findings the way a release note is written. It
# runs on every pair, so its failure mode is a missing report rather than a
# loud one.

echo
echo "notes"

out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/res4.apk" -o "$TMP/rn" 2>&1)
check "the stage runs"        "$out" "stage 8 — release-note draft"
draft="$(cat "$TMP/rn/report/8-notes.md")"
check "and writes a draft"    "$draft" "<!-- apkdiff:draft-begin -->"
# The shell fixtures carry a placeholder manifest, so the version is
# unreadable — which the heading has to say rather than guess at.
check "with a build heading"  "$draft" "## (unknown versionCode)"
check "the resource change"   "$draft" "string/greeting"
check "and its ledger"        "$draft" "evidence ledger"

# --into puts the section in the notes file itself, newest first, and refuses
# to overwrite one somebody may have written by hand.
notesfile="$TMP/notes.md"
printf '# notes\n\nBuilds 1 -> 2.\n\n---\n\n## 1\n\nolder.\n' > "$notesfile"
out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/res4.apk" -o "$TMP/rn2" --notes-into "$notesfile" 2>&1)
check "the section is inserted" "$out" "inserted the"
check "into the file"           "$(cat "$notesfile")" "string/greeting"
check_not "without the scaffolding" "$(cat "$notesfile")" "evidence ledger"
out=$("$APKDIFF" "$TMP/res1.apk" "$TMP/res4.apk" -o "$TMP/rn3" --notes-into "$notesfile" 2>&1)
check "a second run refuses"    "$out" "already has a section for"

# ---------------------------------------------------------------- dex stage

echo
echo "dex"

if [ -x "$DEXBIN" ]; then
  mkdir -p "$TMP/d1" "$TMP/d2"
  mkdex "$TMP/d1/classes.dex"
  mkdex "$TMP/d2/classes.dex" --variant
  mkapk "$TMP/d1"; mkapk "$TMP/d2"

  out=$("$APKDIFF" "$TMP/d1.apk" "$TMP/d2.apk" -o "$TMP/dx" 2>&1)
  check "dex stage uses the direct reader" "$out" "direct reader"
  rep="$(cat "$TMP/dx/report/3-dex.txt")"
  check "summarises the class delta"   "$rep" "1 changed"
  check "names the changed class"      "$rep" "com/example/Thing"
  check "ranks it for reading"         "$rep" "biggest changes first"
  check "lists string literals"        "$rep" "new string literals"

  # The variant fixture changes one method body and no signature, so the API
  # section holds exactly one `~` row. A two-way ternary over three signs
  # printed it as REMOVED — stage 3 saying a method had gone away while stage 7
  # correctly called it a rewrite, in the same run.
  # Matched against the whole report rather than a section, so neither
  # assertion can be satisfied by a heading that moved.
  check "a rewritten body is labelled as one" "$rep" "REWRITTEN .method public greet"
  check_not "and not as a removal"            "$rep" "REMOVED .method public greet"

  # A dex compared with itself must produce an empty delta, not a crash.
  out=$("$APKDIFF" "$TMP/d1.apk" "$TMP/d1.apk" -o "$TMP/dxsame" 2>&1)
  check "identical dex yields no class changes" "$(cat "$TMP/dxsame/report/1-inventory.txt")" "changed=0"

  # An unreadable dex must be reported, not fatal.
  mkdir -p "$TMP/d3"; printf 'not a dex at all, really not' > "$TMP/d3/classes.dex"
  mkapk "$TMP/d3"
  out=$("$APKDIFF" "$TMP/d1.apk" "$TMP/d3.apk" -o "$TMP/dxbad" 2>&1)
  check "a corrupt dex is reported"   "$out" "could not read"
  check "and the run still finishes"  "$out" "reports written to"

  # The reader exits without unwinding, to skip freeing a quarter of a million
  # strings on its way out. That is only safe if nothing buffered is lost, so
  # pin both halves of its output: the summary it prints, and the last line of
  # the largest artefact it writes. A truncating exit would drop either.
  ex_out=$("$DEXBIN" --pool "$TMP/poolexit" \
    --old "$TMP/d1/classes.dex" --new "$TMP/d2/classes.dex" 2>&1)
  check "the reader still prints its summary" "$ex_out" "classes:"
  check "and its member counts"               "$ex_out" "members:"
  check "all six artefacts survive the early exit" \
    "$(ls "$TMP"/poolexit.* 2>/dev/null | wc -l | tr -d ' ')" "6"
  check "the literal artefact ends with a complete line" \
    "$(tail -c 1 "$TMP/poolexit.str.new" | od -An -c | tr -d ' ')" '\n'

  # The dex files are now loaded on one thread each, so the error a run
  # reports is no longer simply the first one it happened to reach. It must
  # still be the first in argument order, whichever thread finishes first:
  # two unreadable shards on the same side must not report the second one.
  bad_out=$("$DEXBIN" --pool "$TMP/poolerr" \
    --old "$TMP/d3/classes.dex" "$TMP/d3/classes.dex" \
    --new "$TMP/d1/classes.dex" 2>&1 || true)
  check "the first unreadable dex in order is the one named" \
    "$bad_out" "$TMP/d3/classes.dex"
  check_not "and the run does not claim success" "$bad_out" "classes:"

  # --java runs jadx over the same pair.
  if command -v jadx >/dev/null; then
    out=$("$APKDIFF" "$TMP/d1.apk" "$TMP/d2.apk" -o "$TMP/dxj" --java 2>&1)
    check "java stage runs" "$out" "stage 5"
    check "java report written" "$(ls "$TMP/dxj/report")" "5-java.txt"
  else
    echo "  -- jadx not installed; skipping the --java case"
  fi
else
  echo "  -- apkdiff-dex not built; skipping the dex stage cases"
fi

# ---------------------------------------------------------------- fallback paths
#
# The Rust helpers are optional, so the Python and apktool routes have to keep
# working. APKDIFF_NO_RUST forces them even when the binaries are built.

echo
echo "fallbacks (APKDIFF_NO_RUST=1)"

out=$(APKDIFF_NO_RUST=1 "$APKDIFF" "$TMP/n1.apk" "$TMP/n2.apk" -o "$TMP/fb-nat" 2>&1)
check "python native helper still runs" "$out" "stage 4"
check "and finds the same string" \
  "$(cat "$TMP/fb-nat/report/4-native.txt")" "could not reach the remote service"

if [ -x "$DEXBIN" ]; then
  out=$(APKDIFF_NO_RUST=1 "$APKDIFF" "$TMP/d1.apk" "$TMP/d2.apk" -o "$TMP/fb-dex" 2>&1)
  check "apktool fallback is announced" "$out" "apktool fallback"
fi

# ---------------------------------------------------------------- real archives
#
# Opportunistic: if full APKs happen to sit next to the script, use them to
# cover the paths that need a genuine binary manifest and a real dex. Skipped
# cleanly when they are absent, so the suite is not tied to them.

echo
echo "real archives (if present)"

# builds/ first, which is where the README puts them and where --intake files
# them; the source root is still searched because a build dropped there is the
# state --intake exists to tidy up, and skipping these cases silently because
# the archives moved one directory is worse than looking in both places.
REAL_A=""; REAL_B=""
for f in "$ROOT"/builds/*.apk_ "$ROOT"/builds/*.apk "$ROOT"/*.apk_ "$ROOT"/*.apk; do
  [ -f "$f" ] || continue
  if [ -z "$REAL_A" ]; then REAL_A="$f"
  elif [ -z "$REAL_B" ]; then REAL_B="$f"; break
  fi
done

if [ -n "$REAL_B" ]; then
  out=$("$APKDIFF" "$REAL_A" "$REAL_B" -o "$TMP/real" 2>&1)
  check "manifest decodes"       "$out" "stage 2"
  check "version is extracted"   "$(cat "$TMP/real/report/7-findings.txt")" "VERSION"
  check "run completes"          "$out" "reports written to"

  # The apktool fallback needs a dex that baksmali accepts, which the minimal
  # generated fixture is not (it omits the map_list and checksum a real
  # toolchain writes). Exercise that path against the real archives instead.
  out=$(APKDIFF_NO_RUST=1 "$APKDIFF" "$REAL_A" "$REAL_B" -o "$TMP/fb-full" 2>&1)
  check "apktool fallback runs"          "$out" "apktool fallback"
  rep="$(cat "$TMP/fb-full/report/3-dex.txt")"
  check "fallback ranks by diff size"    "$rep" "smali diff size"
  check "fallback lists members"         "$rep" "methods & fields"
  check "fallback diffs string literals" "$rep" "new string literals"
  check "fallback reaches findings"      "$(cat "$TMP/fb-full/report/7-findings.txt")" "VERSION"
  # The fallback cannot hash an opcode sequence, so it makes no BEHAVIOUR
  # CHANGE finding. Both reports have to say that rather than leave a silence
  # that reads as "no method was rewritten".
  check "fallback admits it skipped bodies" "$rep" "not detected on"
  check "and findings says so too" \
    "$(cat "$TMP/fb-full/report/7-findings.txt")" "method bodies were not compared"

  # A synthetic dex that baksmali rejects must be reported, not fatal.
  if [ -x "$DEXBIN" ]; then
    mkdir -p "$TMP/r1"
    (cd "$TMP/r1" && unzip -oq "$REAL_A" AndroidManifest.xml)
    mkdex "$TMP/r1/classes.dex"
    mkapk "$TMP/r1"
    mkdir -p "$TMP/r2"
    cp "$TMP/r1/AndroidManifest.xml" "$TMP/r2/"
    mkdex "$TMP/r2/classes.dex" --variant
    mkapk "$TMP/r2"
    out=$(APKDIFF_NO_RUST=1 "$APKDIFF" "$TMP/r1.apk" "$TMP/r2.apk" -o "$TMP/fb-bad" 2>&1)
    check "an undecodable dex is reported" "$(cat "$TMP/fb-bad/report/3-dex.txt")" "apktool failed"
    check "and the run still finishes"     "$out" "reports written to"
  fi

else
  echo "  -- no full APKs alongside the script; skipping"
fi

# A genuine .aab exercises the bundletool manifest route, which no synthetic
# fixture can (its manifest is protobuf, not something writable by hand).
REAL_AAB=""
for f in "$ROOT"/builds/*.aab_ "$ROOT"/builds/*.aab \
         "$ROOT"/*.aab_ "$ROOT"/*.aab; do
  [ -f "$f" ] && { REAL_AAB="$f"; break; }
done
if [ -n "$REAL_AAB" ] && [ -n "$REAL_A" ] && command -v bundletool >/dev/null; then
  out=$("$APKDIFF" "$REAL_A" "$REAL_AAB" -o "$TMP/realaab" 2>&1)
  check "bundle is detected"            "$out" "[bundle]"
  check "bundletool reads the manifest" "$(cat "$TMP/realaab/report/2-manifest.txt")" "/manifest"
  check "mixed packaging is flagged"    "$(cat "$TMP/realaab/report/7-findings.txt")" "MIXED PACKAGING"
else
  echo "  -- no .aab or no bundletool; skipping the bundle manifest case"
fi

# ---------------------------------------------------------------- markers

echo
echo "markers"

mkdir -p "$TMP/t1" "$TMP/t2"
echo "clean file" > "$TMP/t1/notes.txt"
echo "TODO finish this; FIXME later" > "$TMP/t2/notes.txt"
mkapk "$TMP/t1"; mkapk "$TMP/t2"
out=$("$APKDIFF" "$TMP/t1.apk" "$TMP/t2.apk" -o "$TMP/mk" --markers 2>&1)
check "marker stage runs"      "$out" "stage 6"
check "counts TODO hits"       "$out" "TODO:"
check "writes a marker report" "$(cat "$TMP/mk/report/6-markers.txt")" "TODO"

# ---------------------------------------------------------------- bundles

echo
echo "bundles"

mkdir -p "$TMP/bundle/base/dex" "$TMP/bundle/base/lib/arm64-v8a" "$TMP/bundle/base/manifest"
mkdir -p "$TMP/plain/lib/arm64-v8a"
echo "cfg" > "$TMP/bundle/BundleConfig.pb"
echo "manifest bytes" > "$TMP/bundle/base/manifest/AndroidManifest.xml"
mkso "$TMP/bundle/base/lib/arm64-v8a/libx.so" "the quick brown fox jumps over"
mkso "$TMP/plain/lib/arm64-v8a/libx.so"       "the quick brown fox jumps over"
echo "manifest bytes" > "$TMP/plain/AndroidManifest.xml"
mkapk "$TMP/bundle"; mkapk "$TMP/plain"

out=$("$APKDIFF" "$TMP/bundle.apk" "$TMP/plain.apk" -o "$TMP/bun" 2>&1)
check "bundle layout is detected"           "$out" "[bundle]"
check "mixed packaging is announced"        "$out" "normalised"
check "findings flag the mixed comparison"  "$(cat "$TMP/bun/report/7-findings.txt")" "MIXED PACKAGING"
# base/lib/... normalises onto lib/..., so the identical .so must not show up.
check_not "normalised paths match up" "$(cat "$TMP/bun/report/1-inventory.txt")" "libx.so"

# ---------------------------------------------------------------- manifest

echo
echo "manifest"

mkdir -p "$TMP/mf1" "$TMP/mf2"
python3 - "$TMP/mf1/AndroidManifest.xml" 100 <<'EOF'
import sys
sys.path.insert(0, "tests")
from mkaxml import axml
open(sys.argv[1], "wb").write(axml(
    {"package": (0x03, 0, "com.example.app"),
     "android:versionCode": (0x10, int(sys.argv[2]), None)},
    children=[("application", {"android:name": (0x03, 0, "com.acme.thing.App")})]))
EOF
python3 - "$TMP/mf2/AndroidManifest.xml" 101 <<'EOF'
import sys
sys.path.insert(0, "tests")
from mkaxml import axml
open(sys.argv[1], "wb").write(axml(
    {"package": (0x03, 0, "com.example.app"),
     "android:versionCode": (0x10, int(sys.argv[2]), None)},
    children=[("application", {"android:name": (0x03, 0, "com.acme.thing.App")}),
              ("uses-permission", {"android:name": (0x03, 0, "android.permission.CAMERA")})]))
EOF
mkapk "$TMP/mf1"; mkapk "$TMP/mf2"
out=$("$APKDIFF" "$TMP/mf1.apk" "$TMP/mf2.apk" -o "$TMP/mf" 2>&1)
check "the binary manifest is read without apktool" "$out" "android:versionCode=101"
check "version shows up as a finding" \
  "$(cat "$TMP/mf/report/7-findings.txt")" "[VERSION] versionCode 100 -> 101"
check "a new permission is called out" \
  "$(cat "$TMP/mf/report/7-findings.txt")" "[PERMISSION CHANGE] the app requests 1"
check "the app namespace comes from the application class" \
  "$(cat "$TMP/mf/work/appns.txt")" "com/acme/thing"

# A manifest that cannot be decoded must be reported, not crash the run.
mkdir -p "$TMP/badmf1" "$TMP/badmf2"
echo "not a manifest" > "$TMP/badmf1/AndroidManifest.xml"
echo "also not one"   > "$TMP/badmf2/AndroidManifest.xml"
mkapk "$TMP/badmf1"; mkapk "$TMP/badmf2"
out=$("$APKDIFF" "$TMP/badmf1.apk" "$TMP/badmf2.apk" -o "$TMP/badmf" 2>&1)
check "an unreadable manifest is reported" "$out" "could not read one of the manifests"
check "and the run still finishes"         "$out" "reports written to"

# ---------------------------------------------------------------- intake

echo
echo "intake and filing"

mkdir -p "$TMP/store" "$TMP/v1" "$TMP/v2"
mkmanifest "$TMP/v1" 100; mkmanifest "$TMP/v2" 101
echo one > "$TMP/v1/a.txt"; echo two > "$TMP/v2/a.txt"
mkapk "$TMP/v1"; mkapk "$TMP/v2"
mv "$TMP/v1.apk" "$TMP/store/download (1).apk"
mv "$TMP/v2.apk" "$TMP/store/whatever.bin"

out=$("$APKDIFF" --intake "$TMP/store/download (1).apk" "$TMP/store/whatever.bin" 2>&1)
check "intake renames by versionCode"   "$out" "-> "
check "intake reports the package"      "$out" "com.example.app"
check "intake reports the version"      "$out" "versionCode=101"
check "a name with a space is handled"  "$(ls "$TMP/store")" "100.apk"
check "an unrecognised extension too"   "$(ls "$TMP/store")" "101.apk"

out=$("$APKDIFF" --intake 2>&1); check "intake needs a file" "$out" "usage:"
out=$("$APKDIFF" --intake /nope/x.apk 2>&1); check "intake reports a missing file" "$out" "no such file"

# Filing something already correctly named must be a no-op, not an error.
out=$("$APKDIFF" --intake "$TMP/store/100.apk" 2>&1)
check "re-filing an already-filed build is a no-op" "$out" "versionCode=100"

# Two different builds claiming one name must not silently clobber each other.
cp "$TMP/store/101.apk" "$TMP/store/copy.bin"
printf 'x' >> "$TMP/store/copy.bin"
out=$("$APKDIFF" --intake "$TMP/store/copy.bin" 2>&1)
check "a colliding, differing build is refused" "$out" "already exists and differs"
rm -f "$TMP/store/copy.bin"

# An identical file under a second name is filed by pointing at the original.
cp "$TMP/store/101.apk" "$TMP/store/dup.bin"
out=$("$APKDIFF" --intake "$TMP/store/dup.bin" 2>&1)
check "an identical duplicate is left alone" "$out" "already filed as"
rm -f "$TMP/store/dup.bin"

# One file: find the build before it without being told.
out=$("$APKDIFF" "$TMP/store/101.apk" -o "$TMP/auto" 2>&1)
check "one file finds the previous build" "$out" "previous build: 100.apk"
check "and diffs against it"              "$(cat "$TMP/auto/report/1-inventory.txt")" "a.txt"

# Nothing earlier to compare against.
mkdir -p "$TMP/lonely"; mv "$TMP/store/100.apk" "$TMP/lonely/100.apk"
out=$("$APKDIFF" "$TMP/lonely/100.apk" -o "$TMP/none" 2>&1)
check "no earlier build is reported clearly" "$out" "no earlier build"

# --builds points filing and the search somewhere else.
mkdir -p "$TMP/elsewhere" "$TMP/v3"
mkmanifest "$TMP/v3" 102; echo three > "$TMP/v3/a.txt"; mkapk "$TMP/v3"
out=$("$APKDIFF" --intake --builds "$TMP/elsewhere" "$TMP/v3.apk" 2>&1)
check "--builds files into another directory" "$(ls "$TMP/elsewhere")" "102.apk"

# --no-rename analyses in place.
cp "$TMP/store/101.apk" "$TMP/store/keepme.apk"
out=$("$APKDIFF" --no-rename --builds "$TMP/store" "$TMP/store/keepme.apk" -o "$TMP/keep" 2>&1)
check "--no-rename leaves the name alone" "$(ls "$TMP/store")" "keepme.apk"

# A build with no version at all cannot be filed.
mkdir -p "$TMP/nov"; python3 - "$TMP/nov/AndroidManifest.xml" <<'EOF'
import sys
sys.path.insert(0, "tests")
from mkaxml import axml
open(sys.argv[1], "wb").write(axml({"package": (0x03, 0, "com.example.app")}))
EOF
mkapk "$TMP/nov"
out=$("$APKDIFF" --intake "$TMP/nov.apk" 2>&1)
check "a build with no versionCode is refused" "$out" "no versionCode"

# ---------------------------------------------------------------- dex pooling

echo
echo "dex pooling"

if [ -x "$DEXBIN" ]; then
  # The same classes, in classes.dex on one side and classes2.dex on the other,
  # with an empty shard opposite each: R8 re-sharding, and nothing else. Pooled,
  # it must not read as a removal plus an addition.
  mkdir -p "$TMP/s1" "$TMP/s2"
  mkdex "$TMP/s1/classes.dex"
  "$DEXBIN" --emit-fixture "$TMP/s1/classes2.dex" --empty
  "$DEXBIN" --emit-fixture "$TMP/s2/classes.dex" --empty
  mkdex "$TMP/s2/classes2.dex"
  mkapk "$TMP/s1"; mkapk "$TMP/s2"
  out=$("$APKDIFF" "$TMP/s1.apk" "$TMP/s2.apk" -o "$TMP/shard" 2>&1)
  check "both shards are pooled into one comparison" \
    "$(cat "$TMP/shard/report/3-dex.txt")" "pooled 2 old and 2 new"
  check_not "a class that only moved shard is not reported as new" \
    "$(cat "$TMP/shard/report/3-dex.txt")" "NEW\tcom/example/Thing"
  check "findings say nothing about the reshuffle" \
    "$(cat "$TMP/shard/report/7-findings.txt")" "No heuristic findings"
else
  echo "  (skipped: build rust/ to cover the dex reader)"
fi

# ---------------------------------------------------------------- summary

echo
printf 'passed %d, failed %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
