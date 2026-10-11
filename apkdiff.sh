#!/usr/bin/env bash
#
# apkdiff.sh — find what actually changed between two builds of the same app.
#
#   ./apkdiff.sh OLD NEW [-o OUTDIR] [--java] [--markers] [--clean]
#                        [--notes-into docs/<package>-release-notes.md]
#   ./apkdiff.sh NEW                 file NEW, then diff it against the build before it
#   ./apkdiff.sh --intake FILE...    file the given builds and stop
#
# OLD and NEW may each be an .apk or an .aab. Format and version come from the
# archive's contents, never its name, and bundle paths are normalised onto APK
# paths so either packaging compares with either. Only --intake and the one-file
# form rename a build to <versionCode>.<fmt>; --no-rename turns that off and
# --builds DIR files and searches elsewhere.
#
# Stages (each writes a numbered report under OUTDIR/report/):
#   1. inventory  — zip entries compared by CRC
#   2. manifest   — manifests, other binary XML and resources.arsc, flattened and diffed
#   3. dex        — changed dex files pooled; classes, members, body opcodes, strings
#   4. native     — exported symbols and added/removed .rodata log strings
#   5. java       — (--java) jadx diff, cross-checked against the bytecode
#   6. markers    — (--markers) TODO/FIXME/unimplemented sweep of both builds
#   7. findings   — triage: which of the above looks like a change, and why
#   8. notes      — the findings clustered into a release-note draft
#                   (--notes-into FILE inserts it into a notes file)
#
# Requires: unzip zip python3
# Optional: bundletool (.aab manifests), jadx (--java), nm (symbol diff),
#           apktool (only for the dex fallback when the Rust helpers are absent)
# The helpers in lib/ are resolved relative to this script and must stay beside it.
#
# --- Gotchas this script exists to handle -------------------------------------
#
# * `strings foo.so | diff` is useless on Rust/C++ libraries: the linker packs
#   literals into .rodata without separators, so one inserted byte reflows the
#   blob and every line "changes". Stage 4 keeps only sentence-shaped
#   candidates that occur nowhere in the other file.
#
# * jadx silently emits "Method not decompiled" stubs when type inference
#   fails, and whether it fails differs between near-identical builds. Stage 5
#   marks any Java diff whose bytecode is identical as an ARTIFACT.
#
# * Without the Rust helpers, dex goes through apktool's bundled baksmali (there
#   is no standalone baksmali release to depend on). Decoding a 180MB archive for
#   one dex is slow, so the changed dex is repacked into a minimal APK first.
#
# * Kotlin/Compose builds differ everywhere for no reason: synthetic lambdas
#   renumber, R8 renames `$r8$lambda$<hash>`, and traceEventStart carries source
#   line numbers. Stages 3 and 7 separate these from real changes.
#
# * An .aab keeps its manifest as protobuf, which apktool cannot decode; stage 2
#   uses bundletool for those and reads an APK's binary manifest itself, since
#   apktool moves versionCode and the sdk versions out into apktool.yml.

set -uo pipefail

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
sub() { printf '    %s\n' "$*"; }

LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional compiled helpers. Both have Python equivalents that produce the same
# output, so the tool works without them; the Rust ones are just faster, and
# apkdiff-dex additionally removes apktool from the default path entirely.
# Build with: cargo build --release --manifest-path rust/Cargo.toml
RS_NATIVE="$LIB/rust/target/release/apkdiff-native-strings"
RS_DEX="$LIB/rust/target/release/apkdiff-dex"
[ -x "$RS_NATIVE" ] || RS_NATIVE=""
[ -x "$RS_DEX" ] || RS_DEX=""
# Set APKDIFF_NO_RUST=1 to take the pure-Python/apktool route even when the
# binaries are present. The test suite uses this to check both paths agree.
if [ "${APKDIFF_NO_RUST:-0}" = 1 ]; then RS_NATIVE=""; RS_DEX=""; fi

native_strings() {                # same CLI either way
  if [ -n "$RS_NATIVE" ]; then "$RS_NATIVE" "$@"
  else python3 "$LIB/lib/native_strings.py" "$@"; fi
}

# ---------------------------------------------------------------- args

OLD=""; NEW=""; OUT=""; DO_JAVA=0; DO_MARKERS=0; CLEAN=0
INTAKE=0; RENAME=1; BUILDS=""; NOTES_INTO=""; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--out)     OUT="$2"; shift 2 ;;
    --java)       DO_JAVA=1; shift ;;
    --markers)    DO_MARKERS=1; shift ;;
    --clean)      CLEAN=1; shift ;;
    --intake)     INTAKE=1; shift ;;
    --no-rename)  RENAME=0; shift ;;
    --builds)     BUILDS="$2"; shift 2 ;;
    --notes-into) NOTES_INTO="$2"; shift 2 ;;
    -h|--help)    awk 'NR > 1 && (!/^#/ || /^# --- Gotchas/) { exit } NR > 1' "$0" \
                    | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)           die "unknown flag $1" ;;
    *)            EXTRA+=("$1"); shift ;;
  esac
done

for t in unzip zip python3; do
  command -v "$t" >/dev/null || die "missing required tool: $t"
done
for h in axml arsc manifest version findings notes markers native_strings; do
  [ -f "$LIB/lib/$h.py" ] || die "missing helper: $LIB/lib/$h.py"
done

abspath() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd)" "$(basename "$1")"); }

# describe FILE -> "format<TAB>package<TAB>versionCode<TAB>versionName", or fail.
describe() { python3 "$LIB/lib/version.py" "$1"; }

# file_build FILE -> prints the path the build now lives at.
#
# Renaming to <versionCode>.<fmt> is what makes "the previous build" a question
# with an answer. It is a rename within a directory, so it is reversible, and it
# refuses rather than clobbering a different file already sitting on the name.
file_build() {
  local f d fmt ver dst
  f=$(abspath "$1")
  d="${BUILDS:-$(dirname "$f")}"
  IFS=$'\t' read -r fmt _ ver _ < <(describe "$f") \
    || die "cannot identify $1 (not an apk or aab?)"
  [ -n "$ver" ] || die "no versionCode in $1"
  dst="$d/$ver.$fmt"
  if [ "$f" = "$dst" ]; then printf '%s\n' "$dst"; return 0; fi
  if [ -e "$dst" ]; then
    if cmp -s "$f" "$dst"; then
      sub "already filed as $(basename "$dst"); leaving $(basename "$f") alone" >&2
      printf '%s\n' "$dst"; return 0
    fi
    die "$dst already exists and differs from $1 — move one of them aside"
  fi
  mkdir -p "$d"
  mv "$f" "$dst" || die "could not rename $1 -> $dst"
  sub "$(basename "$f")  ->  $(basename "$dst")" >&2
  printf '%s\n' "$dst"
}

# previous_build FILE -> the filed build with the highest version below this one.
previous_build() {
  local f d ver
  f=$(abspath "$1")
  d="${BUILDS:-$(dirname "$f")}"
  IFS=$'\t' read -r _ _ ver _ < <(describe "$f") || return 1
  ls -1 "$d" 2>/dev/null \
    | awk -v cur="$ver" -F. '$1+0 < cur+0 && $1 ~ /^[0-9]+$/ &&
                             ($2 == "apk" || $2 == "aab") {print $1+0 "\t" $0}' \
    | sort -rn | head -1 | cut -f2 | sed "s|^|$d/|"
}

if [ "$INTAKE" = 1 ]; then
  [ "${#EXTRA[@]}" -gt 0 ] || die "usage: $0 --intake FILE..."
  say "intake"
  for f in "${EXTRA[@]}"; do
    [ -f "$f" ] || die "no such file: $f"
    filed=$(file_build "$f")
    IFS=$'\t' read -r fmt pkg ver name < <(describe "$filed")
    sub "$(basename "$filed")  $pkg  versionCode=$ver  versionName=$name  [$fmt]"
  done
  exit 0
fi

case "${#EXTRA[@]}" in
  1) # One build: file it, then find what came before it.
     [ -f "${EXTRA[0]}" ] || die "no such file: ${EXTRA[0]}"
     if [ "$RENAME" = 1 ]; then NEW=$(file_build "${EXTRA[0]}")
     else NEW=$(abspath "${EXTRA[0]}"); fi
     OLD=$(previous_build "$NEW")
     [ -n "$OLD" ] || die "no earlier build found in ${BUILDS:-$(dirname "$NEW")}; \
pass both files explicitly"
     say "previous build: $(basename "$OLD")" ;;
  2) # Two files named explicitly are analysed exactly as given. Filing is a
     # side effect nobody asked for here; it belongs to --intake and to the
     # one-file form, where "which build is this" is the whole question.
     OLD="${EXTRA[0]}"; NEW="${EXTRA[1]}"
     [ -f "$OLD" ] || die "no such file: $OLD"
     [ -f "$NEW" ] || die "no such file: $NEW" ;;
  0) die "usage: $0 [OLD] NEW [-o OUTDIR] [--java] [--markers]" ;;
  *) die "unexpected argument ${EXTRA[2]}" ;;
esac

[ "$DO_JAVA" = 1 ] && { command -v jadx >/dev/null || die "--java needs jadx"; }

OLD=$(cd "$(dirname "$OLD")" && pwd)/$(basename "$OLD")
NEW=$(cd "$(dirname "$NEW")" && pwd)/$(basename "$NEW")
ON=$(basename "$OLD" | sed 's/\.[^.]*$//')
NN=$(basename "$NEW" | sed 's/\.[^.]*$//')
[ "$ON" = "$NN" ] && { ON="${ON}-old"; NN="${NN}-new"; }

# Runs go under one directory rather than scattering apkdiff-* siblings through
# whatever directory the tool was invoked from. It is one line in .gitignore,
# one directory to delete, and it keeps successive runs next to each other.
[ -n "$OUT" ] || OUT="$(pwd)/runs/${ON}-vs-${NN}"
[ "$CLEAN" = 1 ] && rm -rf "$OUT"
mkdir -p "$OUT"/{work,report}
W="$OUT/work"; R="$OUT/report"

# A bundle is identified by its own layout, not by file extension: these
# files arrive with names like 20002242.aab_ that no extension check catches.
# NB: `grep -q` in a pipeline is unsafe under `set -o pipefail` — grep exits on
# the first match, SIGPIPEs unzip, and the pipeline reports failure despite the
# match having been found. Count instead, which drains the input.
isbundle() {
  local n
  n=$(unzip -l "$1" 2>/dev/null | grep -cE ' BundleConfig\.pb$| base/manifest/')
  [ "${n:-0}" -gt 0 ]
}
OLD_BUNDLE=0; NEW_BUNDLE=0
isbundle "$OLD" && OLD_BUNDLE=1
isbundle "$NEW" && NEW_BUNDLE=1

say "old: $OLD$([ "$OLD_BUNDLE" = 1 ] && echo '  [bundle]')"
say "new: $NEW$([ "$NEW_BUNDLE" = 1 ] && echo '  [bundle]')"
say "out: $OUT"
echo

# ---------------------------------------------------------------- stage 1: inventory
#
# Compare by CRC from the zip central directory: entry order and compressed
# size shift for reasons that are not code changes, CRC does not.
#
# Each row is "crc <TAB> normalised-name <TAB> real-name". Normalisation maps a
# bundle's layout onto the APK layout so the two can be joined; the real name is
# kept because that is what has to be handed to unzip later.

crclist() {                       # apk -> crc \t norm \t real
  unzip -v "$1" | awk '
    NF>=8 && $1 ~ /^[0-9]+$/ {
      crc=$7; for(i=1;i<=7;i++) $i=""; sub(/^ +/,"");
      real=$0; norm=real;
      # <module>/{manifest,dex,lib,assets,res,root}/... -> APK-equivalent path
      if (match(norm, /^[A-Za-z0-9_]+\/(manifest|dex|lib|assets|res|root)\//)) {
        n = split(norm, p, "/"); mod = p[1]; kind = p[2];
        rest = substr(norm, length(mod) + length(kind) + 3);
        pre = (mod == "base") ? "" : mod "/";
        if (kind == "manifest")   norm = pre "AndroidManifest.xml";
        else if (kind == "dex")   norm = pre rest;
        else if (kind == "root")  norm = pre rest;
        else                      norm = pre kind "/" rest;
      } else if (norm == "base/resources.pb") {
        norm = "resources.arsc";     # same role, different encoding
      }
      print crc "\t" norm "\t" real;
    }' | sort -t"$(printf '\t')" -k2,2
}

say "stage 1 — inventory"
crclist "$OLD" > "$W/crc.old"
crclist "$NEW" > "$W/crc.new"

cut -f2 "$W/crc.old" | sort -u > "$W/names.old"
cut -f2 "$W/crc.new" | sort -u > "$W/names.new"
comm -23 "$W/names.old" "$W/names.new" > "$W/removed.txt"
comm -13 "$W/names.old" "$W/names.new" > "$W/added.txt"

# changed.tsv: norm \t real-in-old \t real-in-new
join -t"$(printf '\t')" -j2 -o 0,1.1,2.1,1.3,2.3 "$W/crc.old" "$W/crc.new" \
  | awk -F'\t' 'BEGIN{OFS="\t"} $2!=$3 {print $1,$4,$5}' > "$W/changed.tsv"
cut -f1 "$W/changed.tsv" > "$W/changed.txt"

realold() { awk -F'\t' -v n="$1" '$1==n{print $2; exit}' "$W/changed.tsv"; }
realnew() { awk -F'\t' -v n="$1" '$1==n{print $3; exit}' "$W/changed.tsv"; }

{
  echo "entries: old=$(wc -l < "$W/names.old" | tr -d ' ')  new=$(wc -l < "$W/names.new" | tr -d ' ')"
  echo "added=$(wc -l < "$W/added.txt" | tr -d ' ')  removed=$(wc -l < "$W/removed.txt" | tr -d ' ')  changed=$(wc -l < "$W/changed.txt" | tr -d ' ')"
  [ "$OLD_BUNDLE" != "$NEW_BUNDLE" ] && {
    echo "(comparing a bundle against an APK; paths normalised)"
    printf 'mixed\n' > "$W/packaging.txt"
  }
  echo
  for f in added removed changed; do
    [ -s "$W/$f.txt" ] || continue
    echo "--- $f ($(wc -l < "$W/$f.txt" | tr -d ' '))"; sed 's/^/  /' "$W/$f.txt" | head -200; echo
  done
} > "$R/1-inventory.txt"
sed 's/^/    /' "$R/1-inventory.txt" | head -30
echo

CHANGED_DEX=$(grep -E '(^|/)classes[0-9]*\.dex$' "$W/changed.txt" 2>/dev/null || true)
CHANGED_SO=$(grep -E '\.so$'                     "$W/changed.txt" 2>/dev/null || true)

# ---------------------------------------------------------------- helpers

# Extract one entry, and let it appear at its final path only once it is
# whole. unzip writing straight into "$W/$tag" is not safe while anything else
# is looking, and since stage 4's libraries are now fetched in the background
# something else always is: a `[ -f ]` against a file unzip is still inflating
# answers yes to a half-written ELF, and the stage that asked reads a prefix of
# a library as if it were the library. Staging and then renaming removes the
# question — a rename within one filesystem is atomic, so an onlooker sees no
# file or a complete one and never anything between. The staging directory is
# named after the entry rather than the pid: $$ is the parent's pid inside a
# background job, so every job in a wave would stage into one directory, and
# $BASHPID is the first bash-4-only thing in this script — the rest of it runs
# on the 3.2 that ships with macOS. One entry is only ever fetched by one job.
extract_one() {                   # apk tag realentry
  local apk="$1"
  local tag="$2"
  local ent="$3"
  local stage
  stage="$W/.staging.$(printf '%s' "$tag/$ent" | tr / _)"
  rm -rf "$stage"
  mkdir -p "$stage" "$(dirname "$W/$tag/$ent")"
  (unzip -oq -d "$stage" "$apk" "$ent" && mv -f "$stage/$ent" "$W/$tag/$ent") 2>/dev/null
  local rc=$?
  rm -rf "$stage"
  return "$rc"
}

pull() {                          # apk tag realentry -> local path
  local dst="$W/$2/$3"
  [ -f "$dst" ] || extract_one "$1" "$2" "$3"
  echo "$dst"
}

# Warm `pull`'s cache for several entries at once.
#
# Extracting one 32MB library from a 180MB archive costs ~120ms, and the stages
# below want two of them per ABI. Taken one after another that is most of a
# second of a single unzip process inflating while every other core idles —
# unzip is single-threaded, but N unzips are not.
#
# Best-effort by design: a failure here is not reported and not fatal, because
# `pull` still runs afterwards and extracts anything missing itself. This only
# ever moves work earlier, never becomes the only attempt at it.
JOBS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)

prefetch() {                      # apk tag entry [apk tag entry ...]
  local running=0
  while [ "$#" -ge 3 ]; do
    # One `local` per statement: `local a="$1" b="$a"` does not work under
    # `set -u`, which is why this is not one declaration.
    local apk="$1"
    local tag="$2"
    local ent="$3"
    shift 3
    [ -f "$W/$tag/$ent" ] && continue
    extract_one "$apk" "$tag" "$ent" &
    running=$((running + 1))
    # Bounded: a build with dozens of changed libraries would otherwise start
    # dozens of unzips against the same two archives and spend the time seeking.
    [ "$running" -lt "$JOBS" ] || { wait || true; running=0; }
  done
  wait || true
}

# Stage 4's libraries depend on nothing any earlier stage produces, so fetch
# them while stages 2 and 3 run rather than in front of stage 4. On a real pair
# that is ~200ms of unzip hidden under ~270ms of manifest and dex work. It is
# safe to look early because `extract_one` renames each file into place only
# once it is whole; it is still cheap when it loses, because stage 4 waits for
# this pass before running its own, so nothing is ever extracted twice.
NATIVE_PREFETCH_PID=""
NATIVE_WANT=()
if [ -n "$CHANGED_SO" ]; then
  for so in $CHANGED_SO; do
    NATIVE_WANT+=("$OLD" old "$(realold "$so")" "$NEW" new "$(realnew "$so")")
  done
  prefetch "${NATIVE_WANT[@]}" &
  NATIVE_PREFETCH_PID=$!
fi

# apktool refuses to decode a bare .dex, and decoding the whole archive to
# reach one dex wastes minutes. Repack just that dex — as classes.dex, which is
# the name apktool looks for — plus the manifest it insists on.
minidex() {                       # apk tag realdex normdex -> smali dir
  # NB: each `local` is its own statement. `local a="$1" b="$a"` does not work
  # under `set -u`: bash declares every name in the list (as unset) before it
  # assigns any of them, so the back-reference trips the unbound-variable trap
  # and silently aborts the function mid-way.
  local apk="$1" tag="$2" dex="$3" norm="$4"
  local slug
  slug=$(printf '%s' "$norm" | tr '/' '_')
  local stage="$W/$tag-$slug-stage"
  local out="$W/$tag-$slug-smali"
  local mf
  [ -d "$out/smali" ] && { echo "$out/smali"; return; }
  mf=$(unzip -l "$apk" | awk '{print $NF}' | grep -E '(^|/)AndroidManifest\.xml$' | head -1)
  rm -rf "$stage"; mkdir -p "$stage"
  (cd "$stage" \
    && unzip -oq "$apk" "$mf" "$dex" \
    && mv "$dex" classes.dex \
    && { [ "$mf" = AndroidManifest.xml ] || mv "$mf" AndroidManifest.xml; } \
    && zip -qr ../"$tag-$slug.apk" .) >/dev/null 2>&1
  apktool d -f -r -o "$out" "$W/$tag-$slug.apk" >/dev/null 2>&1 \
    || { echo "APKTOOL-FAILED"; return; }
  rm -rf "$stage" "$W/$tag-$slug.apk"
  echo "$out/smali"
}

# Produce a readable AndroidManifest.xml for either packaging.
# ---------------------------------------------------------------- stage 2: manifest

say "stage 2 — manifest"
MF_OK=1
for t in old new; do
  arc=$([ "$t" = old ] && echo "$OLD" || echo "$NEW")
  python3 "$LIB/lib/manifest.py" "$arc" > "$W/manifest.$t.flat" 2>"$W/manifest.$t.err" \
    || MF_OK=0
done
if [ "$MF_OK" = 1 ]; then
  python3 "$LIB/lib/manifest.py" --diff \
    "$W/manifest.old.flat" "$W/manifest.new.flat" > "$R/2-manifest.txt" 2>&1
  # The app's own code namespace, for telling its changes from its dependencies'.
  # Derived, never hardcoded: the <application android:name> class is the one
  # class the app is guaranteed to own, and its package names the namespace
  # better than the manifest's `package` attribute, which is an install-time id
  # and is free to differ from where the code lives — a rename, a white-label
  # build or a vanity id all break the assumption that the two agree. Three
  # segments is the usual reverse-DNS root; keeping more would exclude sibling
  # subpackages.
  awk -F'android:name=' '
    /\/application  android:name=/ {
      n = split($2, seg, ".")
      if (n >= 4) { printf "%s/%s/%s\n", seg[1], seg[2], seg[3]; exit }
    }' "$W/manifest.new.flat" > "$W/appns.txt" 2>/dev/null || true
  if [ -s "$R/2-manifest.txt" ]; then
    head -25 "$R/2-manifest.txt" | sed 's/^/    /'
  else
    sub "manifests identical once flattened"
  fi
else
  cat "$W/manifest.old.err" "$W/manifest.new.err" > "$R/2-manifest.txt" 2>/dev/null
  sub "could not read one of the manifests; see report"
fi

# Binary XML is not only the manifest. res/xml/, res/layout/ and the rest are
# the same AXML format the manifest is, and stage 1 compares them by CRC and
# stops there — so a FileProvider root moving from files/ to ../app_flutter/
# read as "1 non-code file changed" and nothing else. The decoder is already
# here; this points it at the rest of them.
#
# Kept out of 2-manifest.txt deliberately: stage 7 reads that file as the
# manifest diff and reports every +/- line in it as a manifest change.
#
# Skipped when either side is a bundle. An .aab stores resource XML as protobuf,
# which this reader cannot parse at all, so every entry would come back
# undecodable — 921 of them on a real apk-vs-aab pair, which is noise the
# MIXED PACKAGING finding already covers in one line.
awk -F'\t' '$1 ~ /\.xml$/ && $1 !~ /(^|\/)AndroidManifest\.xml$/' \
  "$W/changed.tsv" > "$W/changed-xml.tsv"
if [ "$OLD_BUNDLE" = 1 ] || [ "$NEW_BUNDLE" = 1 ]; then
  [ -s "$W/changed-xml.tsv" ] \
    && sub "resource XML: skipped; a bundle stores it as protobuf"
elif [ -s "$W/changed-xml.tsv" ]; then
  # One interpreter and one open of each archive for the whole set. Per entry
  # it was two of each, which cost 84 seconds where this costs under one.
  python3 "$LIB/lib/manifest.py" --entries "$OLD" "$NEW" "$W/changed-xml.tsv" \
    > "$W/axml.diff" 2>/dev/null || : > "$W/axml.diff"
  cp "$W/axml.diff" "$R/2-resource-xml.txt"
  opaque=$(grep -c '^!' "$W/axml.diff" || true)
  same=$(grep -c '^~re-encoded' "$W/axml.diff" || true)
  seen=$(grep -c '^=== ' "$W/axml.diff" || true)
  sub "resource XML: $((seen - same - opaque)) changed, ${same} re-encoded only, ${opaque} undecodable"
  [ "$((seen - same - opaque))" = 0 ] || grep -v '^[~!]' "$W/axml.diff" \
    | sed 's/^/    /' | head -20
fi

# The resource table, for the same reason and with the same exclusion. It is
# the one file in the archive still written in the developer's own words, so a
# changed value can be quoted rather than inferred — but an .aab stores it as
# protobuf (base/resources.pb, which stage 1 normalises onto this name), which
# this reader cannot parse.
#
# Only when the entry actually differs. On a build whose table is byte-identical
# there is nothing to compare, and flattening both sides to prove it would cost
# two reads of a couple of megabytes to write a report saying nothing happened.
if grep -qx 'resources.arsc' "$W/changed.txt" 2>/dev/null; then
  if [ "$OLD_BUNDLE" = 1 ] || [ "$NEW_BUNDLE" = 1 ]; then
    sub "resources: skipped; a bundle stores the table as protobuf"
  else
    RES_OK=1
    for t in old new; do
      arc=$([ "$t" = old ] && echo "$OLD" || echo "$NEW")
      python3 "$LIB/lib/arsc.py" "$arc" > "$W/res.$t" 2>"$W/res.$t.err" || RES_OK=0
    done
    if [ "$RES_OK" = 1 ]; then
      python3 "$LIB/lib/arsc.py" --diff "$W/res.old" "$W/res.new" > "$R/2-resources.txt"
      sub "resources: $(grep -c '^  [a-z]' "$R/2-resources.txt" || true) value(s) differ \
of $(grep -c '^[a-z]' "$W/res.new" || true) compared"
      head -12 "$R/2-resources.txt" | sed 's/^/    /'
    else
      # Both sides or neither: findings.py compares the two files, and one
      # missing side would report every value in the build as new.
      rm -f "$W/res.old" "$W/res.new"
      cat "$W/res.old.err" "$W/res.new.err" > "$R/2-resources.txt" 2>/dev/null
      sub "could not read one of the resource tables; see report"
    fi
  fi
fi
echo

# ---------------------------------------------------------------- stage 3: dex / smali

if [ -n "$CHANGED_DEX" ] && [ -n "$RS_DEX" ]; then
  # Fast path: read the dex format directly. Everything stage 3 reports —
  # the class list, each class's methods and fields, the string pool, and
  # whether a method body moved — is stated by the file itself. Disassembling
  # to smali text through the JVM to recover it was a detour.
  #
  # All changed dex files are pooled into one comparison rather than compared
  # shard by shard. R8 reassigns classes to dex files between builds, so a
  # per-shard diff reports a class that merely moved as one removal plus one
  # addition, and counts every string it carries as new. Pooling the changed
  # shards is enough: a shard that gained or lost a class necessarily changed.
  say "stage 3 — dex (direct reader)"
  : > "$R/3-dex.txt"
  OLD_DEX=(); NEW_DEX=()
  WANT=()
  for dex in $CHANGED_DEX; do
    WANT+=("$OLD" old "$(realold "$dex")" "$NEW" new "$(realnew "$dex")")
  done
  # Every shard of both builds at once, rather than two at a time per shard:
  # the reader pools them all anyway, so none of them is needed before the rest.
  prefetch "${WANT[@]}"
  for dex in $CHANGED_DEX; do
    sub "reading ${dex}…"
    O="$W/old/$(realold "$dex")"; N="$W/new/$(realnew "$dex")"
    [ -f "$O" ] || O=$(pull "$OLD" old "$(realold "$dex")")
    [ -f "$N" ] || N=$(pull "$NEW" new "$(realnew "$dex")")
    OLD_DEX+=("$O"); NEW_DEX+=("$N")
  done

  slug=dex
  NORM="$W/$slug.norm"; API="$W/$slug.api"; RANK="$W/$slug.rank"; STR="$W/$slug.str"
  if ! "$RS_DEX" --pool "$W/$slug" \
        --old "${OLD_DEX[@]}" --new "${NEW_DEX[@]}" > "$W/$slug.summary" 2>&1; then
    { echo "===================== dex"; cat "$W/$slug.summary"; } >> "$R/3-dex.txt"
    sub "could not read the dex files; see report"
  else
    {
      echo "===================== $(printf '%s ' $CHANGED_DEX)(pooled)"
      cat "$W/$slug.summary"
      echo
      echo "--- changed classes by package"
      cut -f2 "$NORM" | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn | head -30
      echo
      echo "--- biggest changes first (signature + body deltas — start here)"
      # Generated classes renumber wholesale whenever any composable above them
      # is edited, so they always top a raw ranking without meaning anything.
      grep -vE 'ComposableSingletons|\$\$ExternalSyntheticLambda|\$lambda[-$]|\$\$inlined' \
        "$RANK" | head -20 | sed 's/^/  /'
      echo
      echo "  (generated/renumbered classes omitted above: $(grep -cE 'ComposableSingletons|\$\$ExternalSyntheticLambda|\$lambda[-$]|\$\$inlined' "$RANK" || true))"
      echo
      echo "--- structural (new & removed classes)"
      grep -vE '^CHANGED' "$NORM" | sed 's/^/  /' | head -60
      echo
      echo "--- added, removed & rewritten methods & fields on changed classes"
      # Three signs, not two. `~` is a member whose signature held and whose
      # body was rewritten; a two-way ternary printed it as REMOVED, which
      # contradicted the same run's stage 7 and told the reader a method had
      # gone away when it had only been rewired.
      awk -F'\t' '{ if ($1 != last) { print $1; last = $1 }
                    tag = ($2 == "+") ? "ADDED" : ($2 == "-") ? "REMOVED" : "REWRITTEN"
                    printf "  %-9s %s\n", tag, $3 }' "$API" \
        | sed 's/^/  /'
      echo
      echo "--- new string literals"
      diff "$STR.old" "$STR.new" | sed 's/^/  /' || true
      echo
    } >> "$R/3-dex.txt"
  fi
  awk '/^--- biggest changes first/,/^$/' "$R/3-dex.txt" | head -14 | sed 's/^/    /'
  sub "full report: $R/3-dex.txt"
  echo

elif [ -n "$CHANGED_DEX" ]; then
  say "stage 3 — dex (apktool fallback; build the Rust helpers to skip this)"
  command -v apktool >/dev/null \
    || die "the dex fallback needs apktool; install it or build the Rust helpers"
  : > "$R/3-dex.txt"
  printf 'apktool\n' > "$W/no-body-diff.txt"

  # Same pooling as the fast path, done by merging the decoded smali trees:
  # comparing shard by shard would report R8's reshuffling as real change.
  SO="$W/pool-old"; SN="$W/pool-new"
  rm -rf "$SO" "$SN"; mkdir -p "$SO" "$SN"
  for dex in $CHANGED_DEX; do
    sub "decompiling $dex (both builds, concurrently)…"
    # The two decodes are independent, so run them at the same time. Each is a
    # separate JVM; serialising them wasted roughly half of this stage.
    minidex "$OLD" old "$(realold "$dex")" "$dex" > "$W/so.path" 2>/dev/null & p1=$!
    minidex "$NEW" new "$(realnew "$dex")" "$dex" > "$W/sn.path" 2>/dev/null & p2=$!
    wait $p1 $p2 2>/dev/null || true
    o=$(cat "$W/so.path" 2>/dev/null); n=$(cat "$W/sn.path" 2>/dev/null)
    if [ -z "$o" ] || [ -z "$n" ] || [ "$o" = "APKTOOL-FAILED" ] || [ "$n" = "APKTOOL-FAILED" ]; then
      echo "$dex: apktool failed" >> "$R/3-dex.txt"; continue
    fi
    (cd "$o" && tar cf - .) | (cd "$SO" && tar xf -)
    (cd "$n" && tar cf - .) | (cd "$SN" && tar xf -)
  done

  slug=dex
  NORM="$W/$slug.norm"; API="$W/$slug.api"; STR="$W/$slug.str"

  # Normalise diff -rq's two output forms to "STATUS<TAB>class".
  # (Done in awk, not sed: BSD sed on macOS does not understand \t.)
  diff -rq "$SO" "$SN" 2>/dev/null \
    | awk -v o="$SO/" -v n="$SN/" 'BEGIN{OFS="\t"}
        /^Files /   { p=$2; sub(o,"",p); sub(/\.smali$/,"",p); print "CHANGED", p; next }
        /^Only in / { d=$3; sub(/:$/,"",d); f=$4; sub(/\.smali$/,"",f);
                      st = (index(d, n)==1) ? "NEW" : "GONE";
                      sub(o,"",d); sub(n,"",d); print st, d "/" f }' \
    | sort -u > "$NORM"

  {
    echo "===================== $(printf '%s ' $CHANGED_DEX)(pooled)"
    echo
    echo "--- changed classes by package"
    cut -f2 "$NORM" | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn | head -30
    echo
    echo "--- biggest changes first (smali diff size — start reading here)"
    # Class count is a poor guide in Kotlin: one edited function renames
    # dozens of lambda classes. Diff volume points at the actual edit.
    awk -F'\t' '$1=="CHANGED"{print $2}' "$NORM" \
    | while read -r cls; do
        [ -f "$SO/$cls.smali" ] && [ -f "$SN/$cls.smali" ] || continue
        n=$(diff "$SO/$cls.smali" "$SN/$cls.smali" | grep -c '^[<>]')
        printf '%6d  %s\n' "$n" "$cls"
      done | sort -rn | head -20 | sed 's/^/  /'
    echo
    echo "--- structural (new & removed classes)"
    grep -vE '^CHANGED' "$NORM" | sed 's/^/  /' | head -60
    echo
    echo "--- added/removed methods & fields on changed classes"
    # Signature churn is far more legible than instruction diffs, and a new
    # field plus a new method on one class is the usual shape of a fix.
    : > "$API"
    awk -F'\t' '$1=="CHANGED"{print $2}' "$NORM" \
    | while read -r cls; do
        [ -f "$SO/$cls.smali" ] && [ -f "$SN/$cls.smali" ] || continue
        d=$(diff <(grep -E '^\.(method|field)' "$SO/$cls.smali" | sort) \
                 <(grep -E '^\.(method|field)' "$SN/$cls.smali" | sort) \
             | grep -E '^[<>]' || true)
        [ -n "$d" ] || continue
        echo "$cls"; echo "$d" | sed 's/^</  REMOVED/; s/^>/  ADDED  /'
        echo "$d" | awk -v c="$cls" 'BEGIN{OFS="\t"}
          /^[<>]/ { s = (substr($0,1,1) == ">") ? "+" : "-"
                    sub(/^[<>] /,""); print c, s, $0 }' >> "$API"
      done | sed 's/^/  /'
    echo
    # This path cannot see a body rewritten behind an unchanged signature. The
    # signal is an opcode-sequence hash, which the direct dex reader computes
    # from the bytecode; apktool hands back body TEXT, whose labels and .line
    # directives churn on every build. Saying so is the point: a stage 7 with no
    # BEHAVIOUR CHANGE in it must not read as "no method was rewritten" when
    # nothing looked. The marker is what tells stage 7 to say the same.
    echo "  (bodies rewritten behind an unchanged signature are not detected on"
    echo "   this path — build the Rust helpers for those)"
    echo
    echo "--- new string literals in changed classes"
    for tag in old new; do
      d=$([ "$tag" = old ] && echo "$SO" || echo "$SN")
      cut -f2 "$NORM" | sed "s|^|$d/|; s|$|.smali|" | tr '\n' '\0' \
        | xargs -0 grep -hoE '"[^"]{4,160}"' 2>/dev/null | sort -u > "$STR.$tag"
    done
    # Drop Kotlin @Metadata signature blobs and Compose source-line markers:
    # both churn on every build without meaning anything.
    diff <(grep -vE '\\u00|\.kt:[0-9]+' "$STR.old") \
         <(grep -vE '\\u00|\.kt:[0-9]+' "$STR.new") | sed 's/^/  /' || true
    echo
  } >> "$R/3-dex.txt"
  awk '/^--- biggest changes first/,/^$/' "$R/3-dex.txt" | head -14 | sed 's/^/    /'
  sub "full report: $R/3-dex.txt"
  echo
fi

# ---------------------------------------------------------------- stage 4: native

if [ -n "$CHANGED_SO" ]; then
  say "stage 4 — native libraries"
  : > "$R/4-native.txt"
  PRIMARY=""
  # The verify pass needs every ABI's bytes regardless of which library is
  # primary, so there is nothing to be gained by extracting them one at a time;
  # the wave was started back at stage 1 and is usually finished by now. Wait
  # for it before the second call rather than trusting the `[ -f ]` check to
  # notice: a job still running would fail that check and be started again.
  [ -n "$NATIVE_PREFETCH_PID" ] && wait "$NATIVE_PREFETCH_PID" 2>/dev/null
  # Whatever the background pass did not manage — it is best-effort and reports
  # nothing — is extracted here, in parallel, before anything reads a library.
  prefetch "${NATIVE_WANT[@]}"
  for so in $CHANGED_SO; do
    O=$(pull "$OLD" old "$(realold "$so")"); N=$(pull "$NEW" new "$(realnew "$so")")
    if [ -z "$PRIMARY" ]; then
      PRIMARY="$so"
      sub "analysing $so (primary)…"
      {
        echo "===================== $so"
        echo "size: old=$(wc -c < "$O" | tr -d ' ')  new=$(wc -c < "$N" | tr -d ' ')"
        echo
        if command -v nm >/dev/null; then
          echo "--- exported symbol diff"
          nm -D --defined-only "$O" 2>/dev/null | awk '{print $NF}' | sort -u > "$W/sym.old"
          nm -D --defined-only "$N" 2>/dev/null | awk '{print $NF}' | sort -u > "$W/sym.new"
          # An empty list on both sides is not "the exports are identical".
          # It means nm produced nothing: a BSD nm that does not take
          # -D/--defined-only, a library with no dynamic symbol table, a file
          # it could not parse. Claiming a negative result the tool never
          # obtained is worse than admitting it has none, and the findings
          # stage reads these two files.
          if [ ! -s "$W/sym.old" ] && [ ! -s "$W/sym.new" ]; then
            echo "  (nm read no dynamic symbols from either build; no symbol"
            echo "   comparison was made — not evidence that the exports match)"
          elif diff -q "$W/sym.old" "$W/sym.new" >/dev/null; then
            echo "  (identical — $(wc -l < "$W/sym.new" | tr -d ' ') exports)"
          else
            diff "$W/sym.old" "$W/sym.new" | sed 's/^/  /'
          fi
          echo
        fi
        native_strings "$O" "$N" "$W/native"
      } >> "$R/4-native.txt"
    else
      # Same source, different target: verify the primary's findings hold here
      # instead of re-deriving them. Re-scanning every ABI was ~60% of total
      # runtime and produced no new information.
      sub "verifying ${so}…"
      {
        echo "===================== $so"
        native_strings --verify "$O" "$N" "$W/native"
        echo
      } >> "$R/4-native.txt"
    fi
  done
  grep -E '^  [+-] ' "$R/4-native.txt" | head -25 | sed 's/^/    /'
  sub "full report (with rodata context): $R/4-native.txt"
  echo
fi

# ---------------------------------------------------------------- stage 5: java

if [ "$DO_JAVA" = 1 ] && [ -n "$CHANGED_DEX" ]; then
  say "stage 5 — java (jadx; slow)"
  : > "$R/5-java.txt"
  for dex in $CHANGED_DEX; do
    slug=$(printf '%s' "$dex" | tr '/' '_')
    O=$(pull "$OLD" old "$(realold "$dex")"); N=$(pull "$NEW" new "$(realnew "$dex")")
    for tag in old new; do
      src=$([ "$tag" = old ] && echo "$O" || echo "$N")
      [ -d "$W/java-$tag-$slug" ] && continue
      sub "jadx ${tag}/${dex}…"
      # --no-inline-anonymous keeps anonymous classes in their own files, which
      # makes the diff attributable. jadx has no package filter, so this is a
      # whole-dex decompile; that is why the stage is opt-in.
      jadx -d "$W/java-$tag-$slug" --no-res --no-debug-info --no-inline-anonymous \
        "$src" >/dev/null 2>&1 || true
    done

    SO=$(minidex "$OLD" old "$(realold "$dex")" "$dex")
    SN=$(minidex "$NEW" new "$(realnew "$dex")" "$dex")
    {
      echo "===================== $dex"
      echo
      diff -rq "$W/java-old-$slug/sources" "$W/java-new-$slug/sources" 2>/dev/null \
      | sed -n 's|^Files .*/sources/\(.*\) and .*|\1|p' \
      | while read -r rel; do
          cls="${rel%.java}"
          # jadx bails out to a "Method not decompiled" stub when type
          # inference fails, and it can fail on one build but not the other.
          # If the smali is byte-identical, the Java diff is fiction.
          if [ -f "$SO/$cls.smali" ] && [ -f "$SN/$cls.smali" ] \
             && cmp -s "$SO/$cls.smali" "$SN/$cls.smali"; then
            echo "ARTIFACT  $cls   (bytecode identical — jadx output differs only)"
          else
            echo "REAL      $cls"
          fi
        done | sort
      echo
      echo "--- diffs for REAL changes (metadata/line-number noise stripped)"
      diff -rq "$W/java-old-$slug/sources" "$W/java-new-$slug/sources" 2>/dev/null \
      | sed -n 's|^Files .*/sources/\(.*\) and .*|\1|p' \
      | while read -r rel; do
          cls="${rel%.java}"
          [ -f "$SO/$cls.smali" ] && [ -f "$SN/$cls.smali" ] \
            && cmp -s "$SO/$cls.smali" "$SN/$cls.smali" && continue
          echo "########## $rel"
          diff -u \
            <(grep -vE 'JADX INFO: loaded|\\u00|\.kt:[0-9]+|traceEventStart|startReplaceGroup' "$W/java-old-$slug/sources/$rel") \
            <(grep -vE 'JADX INFO: loaded|\\u00|\.kt:[0-9]+|traceEventStart|startReplaceGroup' "$W/java-new-$slug/sources/$rel") \
            | tail -n +3
          echo
        done
    } >> "$R/5-java.txt"
  done
  sub "decompiler artifacts suppressed: $(grep -c '^ARTIFACT' "$R/5-java.txt" || true)"
  sub "full report: $R/5-java.txt"
  echo
fi

# ---------------------------------------------------------------- stage 6: markers

if [ "$DO_MARKERS" = 1 ]; then
  say "stage 6 — TODO / stub markers"
  python3 "$LIB/lib/markers.py" "$OLD" "$NEW" > "$R/6-markers.txt" 2>&1
  for p in TODO FIXME 'not yet implemented' 'An operation is not implemented'; do
    sub "$p: $(grep -c "\[$p\]" "$R/6-markers.txt" || true) hits across both builds"
  done
  sub "full report: $R/6-markers.txt"
  echo
fi

# ---------------------------------------------------------------- stage 7: findings

say "stage 7 — findings"
python3 "$LIB/lib/findings.py" "$W" "$R" | sed 's/^/    /'
echo

# ---------------------------------------------------------------- stage 8: notes

# The same evidence, re-filed the way a release note is written: by the change
# rather than by the kind of evidence. Nothing here is new information — it is
# stage 7's findings clustered, sectioned and rendered as markdown, with a
# ledger saying where each one went. What it cannot write is why any of it
# happened, which is the half a person still has to do.
say "stage 8 — release-note draft"
python3 "$LIB/lib/notes.py" "$W" "$R" ${NOTES_INTO:+--into "$NOTES_INTO"} \
  | sed 's/^/    /'
echo

# ---------------------------------------------------------------- done

say "reports written to $R"
ls -1 "$R" | sed 's/^/    /'
