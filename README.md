# apkdiff

Find what actually changed between two builds of an Android app, without the
source.

Two consecutive releases of a Kotlin/Compose app differ in tens of thousands of
places, almost none of which anybody wrote. R8 renumbers every synthetic lambda,
reassigns classes to dex shards, and renames its accessors after a hash of the
body they wrap; Compose stamps source line numbers into `traceEventStart`; the
linker reflows the whole `.rodata` blob when one byte of one string moves. A
plain diff of two APKs is unreadable, and `strings foo.so | diff` is worse than
useless.

This narrows that to a page you can read:

```
$ ./apkdiff.sh --intake ~/Downloads/20002242.apk_ --builds builds
    20002242.apk_  ->  20002242.apk

$ ./apkdiff.sh builds/20002242.apk                   # diff against the build before it
==> previous build: 20002241.apk
...
com.openbubbles.messaging 2.0.0 (20002242)
8 of 1931 archive entries differ

[CHANGE AREA] every changed class is in com.localob.messaging.app.facetime

[STUB IMPLEMENTED] native: "TODO it didn't work" removed
        replaced by: "Reconnecting didn't work"

[CROSS-LAYER CHANGE] 1 concept(s) added on BOTH the native and Java side
        native 'could not tell the service the call failed'
        java   " failed; the alert closes this window, not the service"

[CHANGED CLASS] FaceTimeInCallService: 1 new field, 2 new methods, 1 body rewritten, 2 new strings
        + private final _callFailed: MutableStateFlow
        + public final callFailed()
        + public final getCallFailed() -> StateFlow
        ~ public constructor <init>()
          loads " failed; ending it"

[NEW USER-FACING TEXT] dex: 1 new message(s) shown to the user
        "The connection to this call was lost and could not be re-established."   [FaceTimeCallScreenKt]

[BEHAVIOUR CHANGE] dex: 10 method(s) rewritten on 3 class(es), same signature
        protected FaceTimeCallActivity.onCreate(Bundle)
        public FaceTimeCallActivity.finishCall()
        ...
```

That is a real result: it is the whole of what one build changed, found in three
seconds.

## Usage

```
./apkdiff.sh OLD NEW [-o OUTDIR] [--java] [--markers] [--clean]
                     [--notes-into docs/<package>-release-notes.md]
./apkdiff.sh NEW                  file NEW, then diff it against the build before it
./apkdiff.sh --intake FILE...     file the given builds and stop
```

Either side may be an `.apk` or an `.aab`, in any combination; bundle paths are
normalised onto their APK equivalents so the two line up entry for entry.

Nothing looks at a filename. The format is decided by the archive's layout and
the version by reading its manifest, because builds arrive as `20002242.apk_`,
`base.aab` and `download (3).apk`. Filing renames a build to
`<versionCode>.apk`, which is what makes "the build before this one" a question
with an answer.

| Flag | |
|---|---|
| `-o DIR` | where to write reports (default `runs/OLD-vs-NEW/`) |
| `--intake` | file the given builds and stop |
| `--no-rename` | analyse in place; do not file |
| `--builds DIR` | file into and search this directory instead of the file's own |
| `--java` | also decompile with jadx and diff the Java (slow) |
| `--markers` | sweep both builds for TODO/FIXME/unimplemented |
| `--clean` | start from an empty output directory |
| `--notes-into FILE` | also insert stage 8's draft section into a release-notes file |

## Stages

Each writes a numbered report under `OUTDIR/report/`.

1. **inventory** — zip entries compared by CRC. Usually narrows 2000 files to under 10.
2. **manifest, binary XML & resources** — both manifests flattened to canonical `path  attr=value` lines and diffed, reconciling the encodings a bundle and an APK disagree on. Every other changed binary-XML entry — `res/xml/`, `res/layout/` — goes through the same decoder into `2-resource-xml.txt`, which is how a FileProvider root moving out of the app's own `files/` directory stops reading as "1 non-code file changed". A changed `resources.arsc` is flattened to `type/name = value` and diffed into `2-resources.txt`: it is the one file in the archive still written in the words the developer chose, so `string/call_failed_title` can be quoted rather than inferred.
3. **dex** — all changed dex files pooled, then compared: class list, member signatures, method-body opcode hashes, and the string pool.
4. **native** — exported-symbol diff plus new and removed `.rodata` log strings, each shown with its surrounding bytes, and each repaired against the other build where the linker packed it against a neighbour.
5. **java** — (`--java`) jadx decompile and diff, cross-checked against the bytecode so decompiler failures do not read as changes.
6. **markers** — (`--markers`) TODO/FIXME/unimplemented sweep of both builds.
7. **findings** — triage: which of the above looks like a change, and why.
8. **notes** — the findings clustered by the change they add up to and written out as a release-note draft, with a ledger of where every finding went.

Stage 7 is a list of detectors, each turning one kind of evidence into a
ranked finding: version and permission changes, the package a change is
confined to, components and URL patterns the app now declares, implemented
stubs, Java methods the library has started naming — a `GetMethodID`
descriptor in `.rodata` is the native side stating which method it intends to
call — new and removed failure paths on both sides, classes and members added,
removed or rewritten behind an unchanged signature, user-facing text added,
removed or reworded, JNI descriptors the library actually asks for, exported
symbols, concepts that appear on both sides of the JNI boundary at once,
binary XML outside the manifest whose content moved, and — so that a quiet
report can be read as "nothing else changed" — the XML that differs by CRC but
not by content, and the archive entries no other stage examines. Adding a
signal means adding a detector to `DETECTORS`, not editing a growing
procedure.

Most of those are organised by kind of evidence, which answers "what sort of
change was this" and not "what happened to this class" — the question a release
note is actually written from. `CHANGED CLASS` answers the second: it joins one
class's new fields, new methods, rewritten bodies and newly loaded messages
under a single heading, and appears only for a class carrying more than one
kind of evidence, since one kind is already reported better by its own rule.

Stage 8 takes that the rest of the way. It groups the classes that moved
together — one package, or a type new in this build threaded through both, or
two shared content words between the messages they print — attaches the native
evidence that names the same thing, guesses a section from the shape of what it
finds, and writes the whole thing as markdown:

```
### Fixed

- **A native code path that was a stub is now implemented: `TODO it didn't work`
  is gone from the library.**
    - **Native.** `TODO it didn't work` is no longer in the library, and
      `Reconnecting didn't work` is new
    - **`FaceTimeInCallService`.**
        - 2 new methods: `callFailed`, `getCallFailed`
        - 1 new field: `_callFailed: MutableStateFlow`
        - loads ` failed; ending it`, `native reported call `
```

What it cannot write is *why* any of it happened, or what the person using the
app saw before it. Every lead sentence it produces is a statement about what
the build contains; a finished note leads with "a call whose media link died
left the app hung, with no way out", and turning the first into the second is
reading the evidence and knowing the app. So the draft is a scaffold with the
facts already in the right places and the sentences still to write, and its
last section is a ledger naming every stage-7 finding and where it went — so
that cutting one stays a decision rather than an oversight.

## Install

```sh
cargo build --release --manifest-path rust/Cargo.toml   # optional, ~10x faster
```

Requires `unzip`, `zip` and `python3`. Optional: `bundletool` for `.aab`
manifests, `jadx` for `--java`, `nm` for the symbol diff, and `apktool` only for
the dex fallback when the Rust helpers are not built.

The Rust helpers are optional because each has a Python equivalent that produces
identical output. They are worth building: they take a run from ~25s to ~3s.

## Layout

```
apkdiff.sh              the driver: stages, extraction, reports
run-tests.sh            every suite, and --coverage for each
pyproject.toml          pytest and coverage settings; not a package
lib/
  axml.py               binary XML (AXML) reader
  arsc.py               resource-table reader: type/name = value, and a diff
  manifest.py           flatten + diff, for a manifest or any AXML entry
  version.py            identify a build by its contents
  findings.py           triage — the detector pipeline behind stage 7
  notes.py              stage 8: the findings as a release-note draft
  markers.py            TODO/stub sweep
  native_strings.py     .rodata scanner (Python fallback)
rust/                   apkdiff-dex, apkdiff-native-strings
tests/                  all three suites
docs/                   release notes written with --notes-into (not shipped)
builds/                 the archives being compared (not committed)
runs/                   output; one directory per pair compared (not committed)
```

Everything above `builds/` is tracked, and nothing else is. `builds/` and
`runs/` are both ignored: the archives are somebody else's release artefacts,
the reports are regenerated by re-running the tool, and between them they are a
couple of gigabytes.

Neither language side has a dependency. The Python helpers import only the
standard library and the Rust crate's `Cargo.lock` has no entries — a tool that
has to run wherever an archive can be unzipped is worth more than one that
resolves a package index first.

## Tests

```sh
./run-tests.sh              # all three suites
./run-tests.sh --coverage   # plus coverage for each
./run-tests.sh --strict     # fail if a suite is skipped for a missing tool
```

Measured with `--coverage`: Python is at 100% line and branch coverage, Rust at
100% line and function coverage (cargo-llvm-cov records no branch data here).
No figure is given for the shell driver: bashcov under current bash reports the
test script itself at 24% although every line of it runs, so its number for
`apkdiff.sh` cannot be trusted either. No test reads a real build: each suite
synthesises its own APKs, AABs, ELF-ish `.so` files, resource tables and a
hand-built valid dex.

## What it is not

The resource table is read, and on the app this was built against it has
nothing to say. `resources.arsc` is byte-identical across all five builds here:
the app draws its interface in Compose and keeps its text in the dex, so not
one of its 452 default-configuration values has ever moved, and the stage —
which only runs when the entry's CRC differs — has never fired on this corpus.
It is there for an app that keeps its text in `strings.xml`, where the table is
the only place both the words a person reads and the name their author gave
them survive the build.

What that reader deliberately does not compare: any configuration but the
default, since a build carries the same string once per locale and comparing
all of them turns one edit into forty identical lines (the count of what was
left out is in the report's header, and a language arriving is its own
finding); the types whose values are file paths the shrinker renames or
references whose numeric target renumbers; and resource ids, which are assigned
in declaration order, so inserting one string shifts every id after it.

Method bodies are compared by opcode sequence, so a change to a numeric
constant alone is invisible. That is deliberate and was measured: folding the
`const` family's immediate operands into the hash took one real pair from 47
rewritten methods to 113, and every one of the 66 additions took a `Composer`
parameter — Compose's `traceEventStart` line-number stamps, not one genuine
constant change. The noise is certain and the signal was nil.

Nothing here defeats obfuscation, and none of it recovers intent. The dex side
is exact — names, signatures and literals are read out of the file. The native
side is inference from log strings in a stripped library: well evidenced as to
*what*, silent as to *where*.

## License

[AGPL-3.0-or-later](LICENSE).
