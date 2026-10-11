# apkdiff — contributor and agent instructions

`apkdiff.sh` finds what actually changed between two builds of an Android app,
with no source tree. [README.md](README.md) is the user-facing description and
is accurate; read it first. This file is what a change to the repo has to
respect, and how to write the notes the tool exists to produce.

## The one problem this repo solves

Two consecutive Kotlin/Compose builds differ in tens of thousands of places,
almost none of which anybody wrote. R8 renumbers synthetic lambdas, reassigns
classes between dex shards and renames accessors after a hash of the body they
wrap; Compose stamps source line numbers into `traceEventStart`; the linker
reflows the whole `.rodata` blob when one byte moves.

**Every design decision here is about discarding compiler-invented difference
without discarding real change.** Judge a proposed change by that: does it
suppress more noise, or surface more signal, without weakening the other? A
change that makes the report longer is usually a regression.

## Running it

```sh
./apkdiff.sh --intake ~/Downloads/<file>       # file a build as <versionCode>.apk
./apkdiff.sh builds/<versionCode>.apk          # diff against the build before it
./apkdiff.sh OLD NEW [-o DIR] [--java] [--markers] [--clean]
./apkdiff.sh OLD NEW --notes-into docs/<package>-release-notes.md
./run-tests.sh [--coverage]
cargo build --release --manifest-path rust/Cargo.toml   # ~10x faster, optional
```

Reports land in `runs/OLD-vs-NEW/report/`, working files in `.../work/`. Both
`builds/` and `runs/` are gitignored and regenerable; never commit either.

## Layout and what owns what

| Path | Owns |
|---|---|
| [apkdiff.sh](apkdiff.sh) | the driver: arg handling, filing, all eight stages, extraction, report assembly |
| [lib/axml.py](lib/axml.py) | binary XML (AXML) → ElementTree — the manifest and every `res/` entry |
| [lib/arsc.py](lib/arsc.py) | `resources.arsc` → `type/name<TAB>value`, and the diff of two of those |
| [lib/manifest.py](lib/manifest.py) | flatten either packaging's manifest — or many AXML entries at once, via `--entries` — to `path  attr=value`, and diff two flattened files |
| [lib/version.py](lib/version.py) | identify a build from its contents → `format\tpackage\tversionCode\tversionName` |
| [lib/findings.py](lib/findings.py) | stage 7 triage: the `Evidence` loader and the `DETECTORS` pipeline |
| [lib/notes.py](lib/notes.py) | stage 8: the findings clustered into changes and written as a release-note draft |
| [lib/markers.py](lib/markers.py) | `--markers` TODO/stub sweep |
| [lib/native_strings.py](lib/native_strings.py) | `.rodata` log-string diff (Python fallback) |
| [rust/src/dex.rs](rust/src/dex.rs) | direct Dalvik reader — classes, members, string pool, body opcode hashes |
| [rust/src/dexdiff.rs](rust/src/dexdiff.rs) | compares two dex inventories, emits stage 3's artefacts |
| [rust/src/native.rs](rust/src/native.rs) | Rust port of `native_strings.py`, byte-for-byte output-compatible |
| [rust/src/testdex.rs](rust/src/testdex.rs) | generated minimal valid dex fixture, plus a one-body-changed variant |
| [tests/mkaxml.py](tests/mkaxml.py), [tests/mkarsc.py](tests/mkarsc.py) | the two binary fixture builders, shared by the Python and shell suites |
| [tests/](tests/) | all three suites (see Tests below) |
| `docs/` | release notes written with `--notes-into`, one file per package (none ship with the repo) |

The helpers must stay beside `apkdiff.sh` — it resolves `lib/` relative to its
own `BASH_SOURCE` and dies if one is missing.

## Invariants — do not break these

**Nothing is identified by filename.** Format comes from the archive's layout
(`isbundle` counts `BundleConfig.pb` / `base/manifest/` entries), version from
the manifest. Builds arrive as `20002242.apk_`, `base.aab`, `download (3).apk`.
Filing renames to `<versionCode>.<fmt>` — that rename is the only reason "the
build before this one" has an answer. Filing never clobbers: an existing
different file on the target name is a fatal error, an identical one is left
alone.

**Two files named explicitly are analysed exactly as given.** Filing is a side
effect of `--intake` and the one-file form only.

**Changed dex files are pooled before comparing, never compared shard by
shard.** R8 reassigns classes between shards; a per-shard diff reports a moved
class as one removal plus one addition and counts all its strings as new. Both
the Rust path (`--pool`) and the apktool fallback do this.

**A method body is compared by its opcode sequence, not its raw bytes.**
Operands are pool indices that renumber whenever anything else in the dex
changes; hashing raw bytes makes identical source look different. The
instruction walk in `dex.rs` is self-validating — it must land exactly on the
end of the bytecode array, and reports `none` rather than a confident wrong
answer if it does not.

**Native strings are compared by whole-file membership, never by position.** A
candidate counts as added only if it occurs nowhere in the other file's raw
bytes. That test is immune to `.rodata` reflow; anything position-dependent is
not.

**Only the primary ABI is fully scanned.** The other ABIs are the same source
for a different target, so `--verify` confirms the primary's conclusions hold
rather than re-deriving them. Re-scanning every ABI was ~60% of runtime for no
new information.

**An entry appears at its final path whole or not at all.** Stage 4's
libraries are fetched from stage 1 onwards, in the background, so every stage
after that is looking at paths another process is still filling — and `[ -f ]`
against a file unzip is inflating says yes to a prefix of a library.
`extract_one` unzips into a staging directory and renames, which is atomic
within one filesystem. Any new fetch goes through it; extracting straight to
the destination is what the shell suite's half-extracted-library case fails on.
Overlapping the fetch with stages 2 and 3 took a real pair from 1.62s to 1.48s,
stage 4 from 542ms to 324ms, with byte-identical reports.

**Both helper paths must agree.** Each Rust helper has a Python equivalent
producing identical output; `APKDIFF_NO_RUST=1` forces the Python/apktool
route, and the suite uses it to check the two paths agree. Adding a signal to
one side means adding it to the other.

**No dependencies, either language.** The Python helpers import only the
standard library; `rust/Cargo.lock` has no entries. A tool that has to run
wherever an archive can be unzipped is worth more than one that resolves a
package index first. Do not add a crate or a pip package.

**Not every helper needs a Rust twin; the two that have one must agree.** The
"both helper paths agree" rule is about the helpers that exist in both
languages because the Python one was too slow — `native_strings` and the dex
reader. [lib/arsc.py](lib/arsc.py) has no Rust port and should not get one: it
reads a 1.8MB table in 45ms including interpreter start, against a run that
takes ~2.2s. A port would be several hundred lines of unexercised code buying
a fraction of a percent, which is the same trade "novelty requires evidence"
rejects everywhere else here.

**Stage 8 states what it cannot do, in the document it writes.** The draft's
lead sentences are descriptive by construction — what the build contains,
never why — and its preamble and ledger say so. A change that makes a lead
sentence assert a cause, a user's experience, or an intention is a regression
however well it reads: that half of a note is written by a person reading the
evidence, and a generated sentence that sounds like it was is worse than no
sentence. The ledger is the mechanical half of the same rule: every stage-7
finding is named in it, and a kind listed in `ABSORBED` must genuinely appear
inside an entry — [tests/test_helpers.py](tests/test_helpers.py) checks the
promise rather than the list, over several build shapes rather than one. That
last part is not decoration: the check ran only against a build rich in dex
changes, and a build whose library only *lost* strings put them on a topic
`attach_native` then discarded — the retention test asked for added strings or
stubs and not for removals — so the draft called the build packaging-only while
the ledger called `REMOVED ERROR HANDLING` absorbed. Anything that decides
whether a topic survives has to test everything a topic can carry.

**A member the compiler had to write is not a change.** Three rules, and each
one is about implication rather than about names: a getter or setter whose
property is in the same finding, the constructor and the data-class trio of a
class whose fields moved, and anything declared `synthetic` — which means "not
present in the source" by definition. Each is only ever suppressed alongside
the member it follows from, so a getter arriving alone is still a class's
surface changing, and a constructor rewritten where no field moved is still a
change to what it does. Removing these took the 20002241 report from 299 lines
to 268 with nothing real lost.

**New signals are new detectors.** Add a function to `DETECTORS` in
[lib/findings.py](lib/findings.py); do not extend a growing procedure. A
detector takes the loaded `Evidence` and yields `Finding(rank, kind, title,
evidence)`. `Evidence` reads each artefact at most once — several detectors
want the same multi-megabyte string pool — so read through it, not with a bare
`open`.

**Reports are the interface.** `dexdiff.rs`'s output shapes match what the
apktool/smali path produced so `findings.py` and the reports keep working;
changing an artefact's shape means changing every reader of it.

## Measured and rejected

Keep these here rather than retrying them: each cost a run against the real
corpus to settle.

- **The resource table reader finds nothing in this app, and that is not a
  reason to remove it.** `resources.arsc` is byte-identical across 20002238 →
  20002243, so the stage — which only runs when stage 1 says the entry's CRC
  moved — has never fired on this corpus, and none of the 452
  default-configuration values it would compare has changed. The app is
  Compose and keeps its text in the dex: "Call failed" does not appear
  anywhere in the table's bytes, in either encoding. It stays because it costs
  nothing when the entry is identical and because it is the only place an app
  that uses `strings.xml` keeps both the words and the developer's name for
  them. Do not wire it into anything that assumes it produced output.

- **Locale variants are counted, not compared.** A real table carries ~31,000
  values in 74 non-default configurations against 452 in the default one.
  Comparing them all turns one edited string into 75 lines saying the same
  thing; the header's `nondefault`/`locales`/`languages` counts keep the
  omission visible, and a language arriving is its own finding.

- **A candidate that spans a packed boundary is repaired against the other
  build, not against another ABI.** The ABIs were measured and are useless for
  this: every string in the new arm64 library is present verbatim in the other
  three, because the linker packs `.rodata` in the same order for each target,
  so a peer cannot tell a real literal from two glued together. The other
  *build* can. A suffix that the other build already contains, joined
  mid-token, is an old literal packed after a new one — cut it; and a candidate
  whose head is then too small to have been a candidate at all is nothing but
  a boundary, so it goes. On 20002241→20002242 that turned `Call failed;
  telling the`, `JVM; cannot report the failed callfailed to spawn threadFT
  CLE` and `rofilesset_profile returned no profile` into `Call failed; telling
  the service`, `no JVM; cannot report the failed call` and nothing — which is
  what a person reading the `.rodata` context wrote by hand.

- **The leftward extension must arrive at a boundary or be thrown away.** It
  walks back over lowercase and spaces while the string stays absent from the
  other build, which recovers the word the 89-character cap swallowed. A walk
  that runs out of budget inside unbroken lowercase never found where the
  literal starts, and keeping what it crossed would glue the previous message's
  tail onto this one — inventing a sentence, which is the one failure this tool
  exists to avoid. A missing first word is a truncation the reader can see; a
  borrowed one is not.

## Gotchas already paid for

These are in the source comments too. Reintroducing one is a regression, and
two of them have bitten more than once.

- **`grep -q` in a pipeline under `set -o pipefail`** — grep exits on first
  match, SIGPIPEs the producer, and the pipeline reports failure despite the
  match. Count with `grep -c` instead, which drains the input.
- **`local a="$1" b="$a"` does not work under `set -u`.** Bash declares every
  name in the list unset before assigning any, so the back-reference trips the
  unbound-variable trap and aborts the function mid-way. One `local` per
  statement.
- **`$var` immediately followed by a non-ASCII byte** (an ellipsis in a
  progress line, say) is read as part of the variable *name* and kills the
  script at runtime under `set -u`. `run-tests.sh` greps for this and fails the
  build; keep that lint fatal and use `${var}`.
- **BSD sed on macOS does not understand `\t`.** Normalise diff output in awk.
- **jadx emits a "Method not decompiled" stub when type inference fails**, and
  it can fail on one build and not the other, producing large fictional Java
  diffs. Stage 5 marks any Java diff whose smali is byte-identical as
  `ARTIFACT`.
- **apktool refuses a bare `.dex` and decoding a 180MB archive to reach one is
  slow** — hence `minidex`, which repacks the single dex as `classes.dex` plus
  a manifest into a minimal APK. There is no standalone baksmali available.
- **apktool cannot read an `.aab` manifest at all** (protobuf), and for an APK
  it hoists `versionCode` and the sdk versions out into `apktool.yml` —
  dropping exactly the fields most worth diffing. Hence `axml.py` for APKs and
  bundletool only for bundles.
- **`bytes in mmap` is an element test**, not a substring search, and silently
  answers False for any multi-byte needle. Use `m.find(needle) >= 0`.
- **Resource XML must not be appended to `2-manifest.txt`.** Stage 7 loads
  that whole file as the manifest diff and reports every `+`/`-` line in it as
  a manifest change, so a `res/xml/` diff written there is attributed to the
  manifest. It goes to `2-resource-xml.txt` and `work/axml.diff` instead, and
  `other_entries` subtracts every entry named there — a file stage 2 decoded is
  no longer one "no other stage looked at".

- **The resource-XML pass stands down for a bundle, and reads each archive
  once.** An `.aab` keeps resource XML as protobuf, which `axml.py` cannot
  parse, so every entry comes back undecodable — 921 of them on a real
  apk-vs-aab pair, which `MIXED PACKAGING` already covers in one line. And the
  decode is one `--entries` call over the whole set: the per-entry shell loop it
  replaced re-opened both 180MB archives and started two interpreters for every
  file, which took that same pair from 25s to 84s.

- **Method bodies are hashed by opcode alone, and the `const` family's
  immediates stay out — measured, not assumed.** Immediates are not pool
  indices, so folding them in looks free of renumbering noise. It is not:
  on 20002242→20002243 it took the rewritten-method count from 47 to 113, and
  all 66 additions took a `Composer` parameter. Those are Compose's
  `traceEventStart` line-number stamps arriving as immediate operands. Certain
  noise, and not one genuine constant change behind it. Do not retry this
  without a corpus where a non-`Composer` method shows up.

- **The cost is extraction and allocation, not the algorithms — and mimalloc
  was measured and left out.** Profiling a real pair put a quarter of the dex
  reader's samples inside the system allocator, and an `unzip` of one 32MB
  library at ~120ms is the single most expensive thing a run does. So the wins
  are: extract everything a stage needs in one parallel pass (2.18s to 1.23s
  end to end), read the shards on their own threads, and exit rather than free
  a map the process is finished with. With dependencies allowed, mimalloc as
  the global allocator is a genuine 1.44x on the dex reader — but only ~6% of a
  run, inside the noise, and it buys that by trading "runs wherever an archive
  can be unzipped" for a C toolchain and a package index. `memchr::memmem` for
  the native searches is 1.06x, which is noise: the set-difference prefilter
  means only a few dozen candidates ever reach a raw-byte search, exactly as
  the comments there claim. Do not add either without a case they actually win.

- **The dex files are read in parallel, and `codegen-units = 1` is not why it
  is fast.** Each shard's inventory walk and each side's pool run on their own
  thread (`std::thread::scope`, no crate), which took the pooled 4-shard read of
  a real 43MB pair from 576ms to 248ms with byte-identical artefacts. The
  tempting toolchain knobs were measured on the same pair and rejected: adding
  `codegen-units = 1` and `panic = "abort"` to the release profile came out
  1.01x *slower* than `opt-level = 3` + `lto = true` alone, head to head over
  10 runs. Do not add them back without a measurement that beats that.

- **The app's own namespace is derived, never hardcoded** — from the
  `<application android:name>` class, not the manifest's `package` attribute,
  which is an install-time id free to differ from where the code lives. With no
  namespace, everything counts as the app's own: reporting nothing is worse
  than reporting a dependency's change.

## Tests

```sh
./run-tests.sh              # rust + python + shell + lints
./run-tests.sh --coverage   # plus coverage for each
```

Three suites — Rust tests in-module and in `rust/tests/`, pytest cases in
[tests/test_helpers.py](tests/test_helpers.py), shell assertions in
[tests/test_apkdiff_sh.sh](tests/test_apkdiff_sh.sh) — plus two lints on
`apkdiff.sh` itself. Python is at 100% line and branch coverage and Rust at
100% line and function coverage; keep them there. bashcov's figure for the
shell driver is not trustworthy (it scores the test script, every line of which
runs, at 24%), so shell coverage is not tracked as a number. A fix and its
regression test land in the same commit, and a found defect becomes a sweep for
its class, not a point fix.

Every suite builds its own miniature fixtures — synthesised APKs, AABs, ELF-ish
`.so` files, and a hand-built valid dex from `testdex.rs`. **No test may depend
on a real build in `builds/`**: those are gigabytes, gitignored, and somebody
else's artefacts. `tests/mkaxml.py` and `tests/mkarsc.py` are the single AXML
and resource-table fixture builders, each used by both the Python suite
(imported) and the shell suite (run as a command), so the two cannot drift.

Each suite skips itself when its tooling is absent rather than failing, and the
Python helpers run as subprocesses under coverage via
[tests/_cov/sitecustomize.py](tests/_cov/sitecustomize.py) — that is how
`apkdiff.sh` invokes them, so that is how they are tested. Config lives in
[pyproject.toml](pyproject.toml), which is tool configuration only; apkdiff is
not a Python package and is never installed.

## Writing release notes

Stage 8 generates the scaffold; the notes are still written. `report/8-notes.md`
already carries the skeleton, the sections, the scope paragraph and every name
and literal in the right place, and `--notes-into docs/<package>-release-notes.md`
inserts its section into the file, newest first, refusing rather than
overwriting a section that already exists. What it cannot do is the half below:
the bold sentence that says what the change means for the person using the app.
Start from the draft, rewrite every lead sentence, merge what is one change,
and move what turns out to be noise into `### Notes` saying so — the draft's
ledger names every finding, so cutting one stays a decision.

`docs/<package>-release-notes.md` is named for the package in its manifest.

### Evidence first

Run the diff, then read every report under `runs/<OLD>-vs-<NEW>/report/`, not
just stages 7 and 8. Stages 7 and 8 rank and group; stages 1–6 are where the string
that explains a finding actually lives. If a claim can't be traced to a line in a
report, it doesn't go in — and never write a build's section from the previous
build's section plus a guess.

### Shape of the file

```markdown
# `<package>` — release notes

Builds <first> → <last> (versionName <x> throughout). Newest first.

Reconstructed by binary-diffing consecutive builds with
[`apkdiff.sh`](../apkdiff.sh) — there is no source tree or official changelog
behind them.

---

## <versionCode>
```

Newest first, `---` between builds, header range line updated whenever a build
is added. Each `## <versionCode>` opens with one or two sentences characterising
the build — what kind of build it is, and the scope evidence that says so:
which archive entries differ, and the `CHANGE AREA` package if there is one.
That is the reader's orientation and the honest bound on everything below it.

> A single-purpose bugfix build. Only `classes3.dex`, the four
> `librust_bridge.so` ABIs, the dexopt profiles and the manifest's
> `versionCode` differ from 20002241, and every changed class is in
> `com.example.messenger.app.calls`.

Then `### New`, `### Changed`, `### Fixed`, `### Notes` — in that order, each
omitted when empty. The oldest build in a file is the baseline: say so in one
line and write no notes for it.

### Shape of an entry

Every entry leads with a **bold sentence of plain English** a reader who has
never opened the app could follow, and only then gives the evidence.

- In `### Fixed`, that sentence says **what was broken and what the user
  experienced**, not what the patch did — "A call whose media link
  died left the app hung, with no way out."
- In `### New` / `### Changed`, what the app can now do, or what moved and why
  it matters.
- Evidence after: class and method names, signature changes, log strings in
  backticks, quoted exactly rather than paraphrased.
- User-facing copy goes in **bold with curly quotes**, attributed to the class
  that loads it — the attribution is what makes it a fact rather than a claim
  about intent.
- A change spanning layers gets one lead sentence and one labelled sub-bullet
  per layer (**Rust.**, **Service.**, **Activity.**, **UI.**).
- Say what the old behaviour was whenever the evidence supports it. A note that
  only describes the new state leaves the reader unable to tell what changed
  for them.

Thorough means every finding is accounted for, not that every finding gets a
paragraph: related findings merge into the behaviour they add up to, and one
that turns out to be noise goes in `### Notes` saying so. Nothing is silently
dropped.

### The two kinds of claim

The README's split is the rule this section exists to enforce. **Dex evidence is
exact** — names, signatures and literals read out of the file — and is stated
flatly. **Native evidence is inference** from log strings in a stripped library:
well evidenced as to *what*, silent as to *where*.

So mark every inference as one, in the sentence that makes it — "most likely
pinned/unpinned", "This looks like a toolchain or dependency rebuild rather than
a behaviour change". Never let an inference about a native code path, or about
*why* something changed, read as a fact about what the binary contains. An
obfuscated Kotlin name change is evidence of a signature change and nothing
more: cite the pair (`-uFdPcIQ` → `-RfXq3Jk`), say what it proves (a parameter
was added), hedge anything past that.

### What does not go in

Never report R8 or toolchain noise as change — re-sharded classes, renumbered
lambdas, accessor renames, reflowed `.rodata`. The tool exists to suppress
these; a note reintroducing them is worse than no note. When such a non-change
is big enough that a reader would otherwise wonder, put it in `### Notes` and
name it as a non-change:

> jsoup and osmdroid moved between dex files. That is R8 re-sharding, not new
> code; because all changed dex files are pooled before comparing, the move
> produces no findings at all.

`### Notes` is also where a genuine negative result goes — the libraries were
rebuilt and *no* log string and *no* exported symbol changed across all 956
exports. Write that down, with its numbers.

Also out: file sizes, entry counts and CRCs as content (the opening paragraph
carries the scope), and any speculation about the developers' intentions,
roadmap or reasons.

## Commits

Small and single-topic, imperative subject ≤72 chars saying *what*, body saying
*why*; readable from `git log --oneline` without opening diffs — "Report one
class's change under one heading", "Say which package the change is confined
to". Never `--no-verify`: if a gate is wrong, fix the gate.
