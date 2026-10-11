"""Tests for the Python helpers that apkdiff.sh drives.

The helpers are scripts, not importable modules — two of them run at import
time by design — so each is exercised the way the shell exercises it: as a
subprocess with real files. `runpy` is used where a function needs to be
reached directly.
"""
import os
import re
import runpy
import struct
import subprocess
import sys
import zipfile

import pytest

from mkaxml import axml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

MANIFEST = os.path.join(ROOT, "lib", "manifest.py")
MARKERS = os.path.join(ROOT, "lib", "markers.py")
FINDINGS = os.path.join(ROOT, "lib", "findings.py")
NATIVE = os.path.join(ROOT, "lib", "native_strings.py")


def run(script, *args, expect_ok=True):
    p = subprocess.run(
        [sys.executable, script, *args], capture_output=True, text=True
    )
    if expect_ok:
        assert p.returncode == 0, p.stderr
    return p


def reported(out, sign="+"):
    """The phrases a report names, without the rodata context lines.

    The context deliberately shows raw surrounding bytes, so asserting against
    the whole output would match text the tool did not actually report.
    """
    return [
        l.strip()[2:].strip()
        for l in out.splitlines()
        if l.startswith(f"  {sign} ")
    ]


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text) if isinstance(text, str) else p.write_bytes(text)
    return str(p)


# ---------------------------------------------------------------- manifest

SIMPLE = """<?xml version="1.0"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
          android:versionCode="1" package="com.example">
  <uses-sdk android:minSdkVersion="26"/>
  <application android:label="App">
    <activity android:name="com.example.Main" android:launchMode="2"/>
  </application>
</manifest>
"""


def test_manifest_flattens_to_sorted_lines(tmp_path):
    out = run(MANIFEST, write(tmp_path, "m.xml", SIMPLE)).stdout.splitlines()
    assert out == sorted(out)
    assert "/manifest  android:versionCode=1" in out
    assert "/manifest  package=com.example" in out
    assert any("android:launchMode=2" in l for l in out)


def test_manifest_drops_build_tool_metadata(tmp_path):
    xml = SIMPLE.replace(
        'package="com.example"',
        'package="com.example" android:compileSdkVersion="37" '
        'platformBuildVersionCode="37"',
    )
    out = run(MANIFEST, write(tmp_path, "m.xml", xml)).stdout
    assert "compileSdkVersion" not in out
    assert "platformBuildVersion" not in out


def test_manifest_disambiguates_siblings_by_name(tmp_path):
    xml = SIMPLE.replace(
        '<activity android:name="com.example.Main" android:launchMode="2"/>',
        '<activity android:name="A" android:x="1"/><activity android:name="B" android:x="2"/>',
    )
    out = run(MANIFEST, write(tmp_path, "m.xml", xml)).stdout
    assert "/activity[A]  android:x=1" in out
    assert "/activity[B]  android:x=2" in out


def keys(out):
    """The diff keys of a flattened manifest — one per line, and the thing the
    diff joins the two sides on."""
    ns = runpy.run_path(MANIFEST, run_name="not_main")
    return [ns["split"](l)[0] for l in out.splitlines() if l]


def test_manifest_keeps_every_unnamed_sibling(tmp_path):
    """<data> elements carry no android:name. Keyed by tag alone they all land
    on one diff key and the diff keeps whichever came last, so every deep link
    but that one disappears — silently, which is the worst way to lose a fact."""
    def doc(hosts):
        inner = "".join('<data android:scheme="https" android:host="%s"/>' % h
                        for h in hosts)
        return SIMPLE.replace(
            '<activity android:name="com.example.Main" android:launchMode="2"/>',
            '<activity android:name="com.example.Main"><intent-filter>'
            '<action android:name="android.intent.action.VIEW"/>'
            "%s</intent-filter></activity>" % inner,
        )

    old = run(MANIFEST, write(tmp_path, "a.xml", doc(["a.example", "b.example"]))).stdout
    stated = [l.rsplit("  ", 1)[1] for l in old.splitlines() if "  android:host=" in l]
    assert sorted(stated) == ["android:host=a.example", "android:host=b.example"]
    assert len(set(keys(old))) == len(keys(old)), "two lines share a diff key"

    new = run(MANIFEST, write(tmp_path, "b.xml",
                              doc(["a.example", "b.example", "c.example"]))).stdout
    write(tmp_path, "a.flat", old)
    write(tmp_path, "b.flat", new)
    out = run(MANIFEST, "--diff", str(tmp_path / "a.flat"),
              str(tmp_path / "b.flat")).stdout.splitlines()
    assert [l for l in out if l.startswith("+")] == [
        l for l in out if "android:host=c.example" in l
    ], out
    assert not [l for l in out if l.startswith("-")], out


def test_manifest_tells_two_intent_filters_apart(tmp_path):
    """Neither filter has an attribute of its own; the actions inside identify
    them, so the second filter's children do not overwrite the first's."""
    xml = SIMPLE.replace(
        '<activity android:name="com.example.Main" android:launchMode="2"/>',
        '<activity android:name="com.example.Main">'
        '<intent-filter><action android:name="VIEW"/>'
        '<data android:scheme="https"/></intent-filter>'
        '<intent-filter><action android:name="SEND"/>'
        '<data android:mimeType="text/plain"/></intent-filter>'
        "</activity>",
    )
    out = run(MANIFEST, write(tmp_path, "m.xml", xml)).stdout
    assert "/intent-filter[VIEW]/data  android:scheme=https" in out
    assert "/intent-filter[SEND]/data  android:mimeType=text/plain" in out
    assert len(set(keys(out))) == len(keys(out)), "two lines share a diff key"


def test_manifest_renders_an_element_with_no_attributes(tmp_path):
    xml = '<manifest><queries/></manifest>'
    out = run(MANIFEST, write(tmp_path, "m.xml", xml)).stdout.splitlines()
    assert "/manifest/queries" in out
    assert "/manifest" in out


def test_manifest_names_are_resolved_once_per_distinct_name(tmp_path):
    """Flattening must not cost more as an element gains siblings.

    Disambiguating a repeated tag used to re-count the sibling list for every
    child, each count re-resolving the tag's namespace — quadratic in a number
    nothing bounds, and an <application> carries every component in the app.
    Timing would be flaky, so count the work instead: the namespace regex runs
    once per distinct name, however many elements carry it.
    """
    import xml.etree.ElementTree as ET

    ns = runpy.run_path(MANIFEST, run_name="not_main")
    calls = []
    # `short` reads NS from the globals it was defined in, which is not the
    # dict run_path hands back, so patch the one the function actually sees.
    short = ns["short"]
    g = getattr(short, "__wrapped__", short).__globals__
    real = g["NS"]

    class Counting:
        def match(self, name):
            calls.append(name)
            return real.match(name)

    g["NS"] = Counting()

    NAME = "{http://schemas.android.com/apk/res/android}name"

    def flatten_with(n):
        root = ET.Element("manifest")
        app = ET.SubElement(root, "application")
        for i in range(n):
            ET.SubElement(app, "activity").set(NAME, "com.example.A%d" % i)
        out = []
        calls.clear()
        getattr(short, "cache_clear", lambda: None)()
        ns["walk"](root, "", out)
        return out, len(calls)

    small, small_calls = flatten_with(4)
    large, large_calls = flatten_with(400)
    assert len(small) == 6 and len(large) == 402, "the flattening still works"
    assert "/manifest/application/activity[com.example.A399]  android:name=" \
        "com.example.A399" in large
    # Two distinct namespaced names in the document (the tags are unqualified,
    # android:name is not), so the count is a constant, not a function of n.
    assert small_calls == large_calls <= 4, (small_calls, large_calls)


def test_manifest_keeps_foreign_namespaces_short(tmp_path):
    xml = ('<manifest xmlns:t="http://example.com/t" t:thing="1"/>')
    out = run(MANIFEST, write(tmp_path, "m.xml", xml)).stdout
    assert "thing=1" in out


def test_manifest_diff_reports_real_changes(tmp_path):
    a = write(tmp_path, "a.flat", "/manifest  android:versionCode=1\n/manifest  x=same\n")
    b = write(tmp_path, "b.flat", "/manifest  android:versionCode=2\n/manifest  x=same\n")
    out = run(MANIFEST, "--diff", a, b).stdout
    assert "-/manifest  android:versionCode=1" in out
    assert "+/manifest  android:versionCode=2" in out
    assert "x=same" not in out


def test_manifest_diff_suppresses_numeric_versus_symbolic_encodings(tmp_path):
    # bundletool prints launchMode=2; apktool prints singleTask. Same value.
    a = write(tmp_path, "a.flat", "/m  android:launchMode=2\n")
    b = write(tmp_path, "b.flat", "/m  android:launchMode=singleTask\n")
    assert run(MANIFEST, "--diff", a, b).stdout == ""


def test_manifest_diff_keeps_two_symbolic_values_that_differ(tmp_path):
    a = write(tmp_path, "a.flat", "/m  android:launchMode=singleTask\n")
    b = write(tmp_path, "b.flat", "/m  android:launchMode=singleTop\n")
    assert "singleTop" in run(MANIFEST, "--diff", a, b).stdout


def test_manifest_diff_keeps_two_numeric_values_that_differ(tmp_path):
    a = write(tmp_path, "a.flat", "/m  android:versionCode=1\n")
    b = write(tmp_path, "b.flat", "/m  android:versionCode=2\n")
    assert "+/m  android:versionCode=2" in run(MANIFEST, "--diff", a, b).stdout


def test_manifest_diff_handles_added_and_removed_lines(tmp_path):
    a = write(tmp_path, "a.flat", "/only-old  x=1\n")
    b = write(tmp_path, "b.flat", "/only-new  y=2\n")
    out = run(MANIFEST, "--diff", a, b).stdout
    assert "-/only-old  x=1" in out
    assert "+/only-new  y=2" in out


def test_manifest_diff_handles_valueless_lines(tmp_path):
    a = write(tmp_path, "a.flat", "/manifest/queries\n")
    b = write(tmp_path, "b.flat", "")
    assert "-/manifest/queries" in run(MANIFEST, "--diff", a, b).stdout


def test_manifest_diff_of_identical_files_is_silent(tmp_path):
    a = write(tmp_path, "a.flat", "/m  x=1\n")
    assert run(MANIFEST, "--diff", a, a).stdout == ""


# ---------------------------------------------------------------- markers

def make_zip(path, entries):
    import zipfile

    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return str(path)


def test_markers_finds_each_pattern(tmp_path):
    a = make_zip(tmp_path / "a.apk", {"classes.dex": b"nothing here at all"})
    b = make_zip(
        tmp_path / "b.apk",
        {
            "classes.dex": b"x TODO fix this y",
            "lib/x.so": b"FIXME later; XXX: hmm; HACK around it",
            "other": b"not yet implemented and An operation is not implemented: why",
        },
    )
    out = run(MARKERS, a, b).stdout
    for pat in ("TODO", "FIXME", "XXX:", "HACK", "not yet implemented",
                "An operation is not implemented"):
        assert f"[{pat}]" in out, pat
    assert "classes.dex @0x" in out


def test_markers_prints_a_header_per_archive(tmp_path):
    a = make_zip(tmp_path / "a.apk", {"f": b"clean"})
    out = run(MARKERS, a).stdout
    assert a in out
    assert "=" * 70 in out


def test_markers_skips_enormous_members(tmp_path, monkeypatch):
    # The size guard exists so a 200MB+ member cannot be slurped into memory.
    import zipfile

    p = tmp_path / "big.apk"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("huge", b"\0" * (200 * 1024 * 1024 + 1))
    out = run(MARKERS, str(p)).stdout
    assert "huge" not in out


# ---------------------------------------------------------------- native strings

def test_native_reports_an_added_message(tmp_path):
    old = write(tmp_path, "old.so", b"\x00the quick brown fox jumps\x00")
    new = write(
        tmp_path, "new.so",
        b"\x00the quick brown fox jumps\x00could not tell the service it failed\x00",
    )
    out = run(NATIVE, old, new).stdout
    assert "=== ADDED strings (1) ===" in out
    assert "could not tell the service it failed" in out
    assert "=== REMOVED strings (0) ===" in out


def test_native_reports_a_removed_message(tmp_path):
    old = write(tmp_path, "old.so", b"\x00this message went away entirely\x00")
    new = write(tmp_path, "new.so", b"\x00\x00")
    out = run(NATIVE, old, new).stdout
    assert "=== REMOVED strings (1) ===" in out
    assert "- this message went away entirely" in out


def test_native_spills_for_the_findings_stage(tmp_path):
    old = write(tmp_path, "old.so", b"\x00the quick brown fox jumps\x00")
    new = write(
        tmp_path, "new.so",
        b"\x00the quick brown fox jumps\x00could not tell the service it failed\x00",
    )
    prefix = str(tmp_path / "spill")
    run(NATIVE, old, new, prefix)
    assert "could not tell the service it failed" in open(prefix + ".added.txt").read()
    assert open(prefix + ".removed.txt").read().strip() == ""


def test_native_cancels_first_character_reflow_pairs(tmp_path):
    old = write(tmp_path, "old.so", b"\x00kclient is closed for now\x00")
    new = write(tmp_path, "new.so", b"\x00lclient is closed for now\x00")
    out = run(NATIVE, old, new).stdout
    assert "=== ADDED strings (0) ===" in out
    assert "=== REMOVED strings (0) ===" in out


def test_native_ignores_serde_schema_churn(tmp_path):
    old = write(tmp_path, "old.so", b"\x00struct variant Foo with 2 elements aa\x00")
    new = write(tmp_path, "new.so", b"\x00struct variant Bar with 3 elements bb\x00")
    out = run(NATIVE, old, new).stdout
    assert "ADDED strings (0)" in out


def test_native_verify_accepts_and_rejects(tmp_path):
    same = write(tmp_path, "x.so", b"\x00identical content here\x00")
    prefix = str(tmp_path / "spill")
    open(prefix + ".added.txt", "w").write("a phrase not present\n")
    open(prefix + ".removed.txt", "w").write("")
    out = run(NATIVE, "--verify", same, same, prefix).stdout
    assert "MISMATCH (added)" in out and "1 mismatch(es)" in out

    open(prefix + ".added.txt", "w").write("")
    assert "OK" in run(NATIVE, "--verify", same, same, prefix).stdout


def test_native_verify_flags_a_removed_string_still_present(tmp_path):
    same = write(tmp_path, "x.so", b"\x00identical content here\x00")
    prefix = str(tmp_path / "spill")
    open(prefix + ".added.txt", "w").write("")
    open(prefix + ".removed.txt", "w").write("identical content here\n")
    out = run(NATIVE, "--verify", same, same, prefix).stdout
    assert "MISMATCH (removed)" in out


def test_native_verify_tolerates_missing_spill_files(tmp_path):
    same = write(tmp_path, "x.so", b"\x00abc\x00")
    out = run(NATIVE, "--verify", same, same, str(tmp_path / "absent")).stdout
    assert "OK" in out


def test_native_context_handles_an_absent_needle(tmp_path):
    # context() returns (None, '') when the phrase is not present; reached via
    # runpy because the module has no importable entry point.
    old = write(tmp_path, "old.so", b"\x00abc\x00")
    sys.argv = [NATIVE, old, old]
    ns = runpy.run_path(NATIVE, run_name="not_main")
    assert ns["context"](b"abc", b"zzz") == (None, "")


# ---------------------------------------------------------------- findings

def findings(tmp_path, work=None, report=None):
    w = tmp_path / "work"
    r = tmp_path / "report"
    w.mkdir(exist_ok=True)
    r.mkdir(exist_ok=True)
    for name, text in (work or {}).items():
        f = w / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    for name, text in (report or {}).items():
        (r / name).write_text(text)
    out = run(FINDINGS, str(w), str(r)).stdout
    return out


def test_findings_says_so_when_there_is_nothing(tmp_path):
    assert "No heuristic findings" in findings(tmp_path)


def test_findings_names_the_build_it_is_about(tmp_path):
    """A findings list alone does not say which app or which version, and the
    reader should not have to open two other reports to find out."""
    out = findings(
        tmp_path,
        work={"manifest.new.flat": "/manifest  package=com.example.app\n"
                                   "/manifest  android:versionName=2.1.0\n"
                                   "/manifest  android:versionCode=41\n"
                                   "/manifest/application  android:label=App\n",
              "names.new": "\n".join("entry%d" % i for i in range(300)) + "\n",
              "changed.txt": "classes3.dex\nclasses4.dex\n"},
    )
    assert out.splitlines()[0] == "com.example.app 2.1.0 (41)"
    assert out.splitlines()[1] == "2 of 300 archive entries differ"


def test_findings_headline_states_what_it_knows_and_no_more(tmp_path):
    out = findings(tmp_path, work={"changed.txt": "assets/x.bin\n"})
    assert out.splitlines()[0] == "1 archive entry differs"


def test_findings_reports_version_and_manifest_changes(tmp_path):
    out = findings(
        tmp_path,
        report={
            "2-manifest.txt": "-/m  android:versionCode=1\n"
            "+/m  android:versionCode=2\n"
            "-/m  android:versionName=1.0\n"
            "+/m  android:versionName=2.0\n"
            "+/m/uses-permission  android:name=CAMERA\n"
            "+/m/application/activity  android:exported=true\n"
        },
    )
    assert "[VERSION] versionCode 1 -> 2" in out
    assert "[VERSION] versionName 1.0 -> 2.0" in out
    assert "[PERMISSION CHANGE] the app requests 1 permission(s)" in out
    assert "CAMERA" in out
    # A permission has its own finding, so it must not be counted twice.
    assert "[MANIFEST CHANGE] 1 manifest line(s)" in out
    assert "android:exported=true" in out


def test_findings_ignores_platform_build_version_noise(tmp_path):
    out = findings(
        tmp_path,
        report={"2-manifest.txt": "-/m  platformBuildVersionCode=1\n"
                                  "+/m  platformBuildVersionCode=2\n"},
    )
    assert "MANIFEST CHANGE" not in out


def test_findings_flags_mixed_packaging(tmp_path):
    out = findings(tmp_path, work={"packaging.txt": "mixed\n"})
    assert "[MIXED PACKAGING]" in out


def test_findings_reports_an_implemented_stub_and_its_successor(tmp_path):
    out = findings(
        tmp_path,
        work={
            "native.removed.txt": "TODO it didn't work\n",
            "native.added.txt": "Reconnecting didn't work\n",
        },
    )
    assert "[STUB IMPLEMENTED]" in out
    assert "Reconnecting didn't work" in out


def test_findings_stub_without_a_successor(tmp_path):
    out = findings(tmp_path, work={"native.removed.txt": "TODO some old thing\n"})
    assert "no obvious successor string" in out


def test_findings_reports_native_failure_paths(tmp_path):
    out = findings(
        tmp_path,
        work={"native.added.txt": "could not tell the service it failed\n"},
    )
    assert "[NEW ERROR HANDLING] native" in out


def test_findings_reports_new_members_and_ignores_generated_churn(tmp_path):
    api = "\n".join([
        "com/x/Real\t+\t.method public callFailed()V",
        "com/x/Real\t+\t.field private final flag:Z",
        # renumbered: same signature modulo digits appears on both sides
        "com/x/Real\t-\t.method public lambda$1()V",
        "com/x/Real\t+\t.method public lambda$2()V",
        # generated class: excluded wholesale
        "com/x/ComposableSingletons$Foo\t+\t.method public getLambda-9()V",
        # lambda plumbing: excluded by name
        "com/x/Real\t+\t.method public invoke()V",
        # R8 hash-named accessor: excluded
        "com/x/Real\t+\t.method public static synthetic $r8$lambda$abc()V",
    ]) + "\n"
    out = findings(tmp_path, work={"classes3.dex.api": api})
    assert "[NEW API SURFACE] classes3.dex: 1 new method(s)" in out
    assert "public Real.callFailed()" in out
    assert "[NEW STATE] classes3.dex: 1 new field(s)" in out
    assert "lambda$2" not in out
    assert "getLambda-9" not in out
    assert "invoke()V" not in out
    assert "$r8$lambda$abc" not in out


def test_findings_names_the_package_a_change_is_confined_to(tmp_path):
    norm = ("CHANGED\tcom/x/app/calls/Call\n"
            "NEW\tcom/x/app/calls/ui/Screen\n")
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.norm": norm})
    assert "[CHANGE AREA] every changed class is in com.x.app.calls" in out


def test_findings_says_nothing_about_area_when_the_change_is_spread(tmp_path):
    """A prefix no deeper than the namespace every app class already shares
    states only that the app changed, which the reader knew already."""
    norm = ("CHANGED\tcom/x/app/calls/Call\n"
            "CHANGED\tcom/x/app/settings/Prefs\n")
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.norm": norm})
    assert "CHANGE AREA" not in out


def test_findings_area_ignores_dependency_classes(tmp_path):
    """Otherwise a single androidx class drags the common prefix to nothing."""
    norm = ("CHANGED\tcom/x/app/calls/Call\n"
            "CHANGED\tandroidx/compose/runtime/Composer\n")
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.norm": norm})
    assert "[CHANGE AREA] every changed class is in com.x.app.calls" in out


def test_findings_joins_one_class_evidence_under_one_heading(tmp_path):
    """The per-kind rules scatter a single class's change across four sections.
    A class carrying several kinds at once is the unit a release note is
    written in, so it gets a heading of its own."""
    api = "\n".join([
        "com/x/app/Service\t+\t.field private final _failed:Lkotlinx/Flow;",
        "com/x/app/Service\t+\t.method public final failed()V",
        "com/x/app/Service\t~\t.method public onCreate()V",
    ]) + "\n"
    out = findings(tmp_path, work={
        "appns.txt": "com/x/app\n",
        "classes3.dex.api": api,
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"the call ended unexpectedly"\n',
        "classes3.dex.owner": '"the call ended unexpectedly"\tcom/x/app/Service\n',
    })
    assert ("[CHANGED CLASS] Service: 1 new field, 1 new method, "
            "1 body rewritten, 1 new string") in out
    assert "+ private final _failed: Flow" in out
    assert "~ public onCreate()" in out
    assert 'loads "the call ended unexpectedly"' in out


def test_findings_does_not_repeat_a_class_with_only_one_kind_of_evidence(tmp_path):
    """Three new methods and nothing else is already legible in NEW API
    SURFACE; restating it under a second heading is length, not information."""
    api = "\n".join([
        "com/x/app/Only\t+\t.method public a()V",
        "com/x/app/Only\t+\t.method public b()V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "[NEW API SURFACE]" in out
    assert "CHANGED CLASS" not in out


def test_findings_caps_the_evidence_listed_per_class(tmp_path):
    """A class that gained thirty methods must not print thirty lines here:
    the digest exists to be read before the exhaustive per-kind sections."""
    rows = ["com/x/app/Big\t+\t.field private final f:Z"]
    rows += ["com/x/app/Big\t+\t.method public m%d()V" % i for i in range(9)]
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": "\n".join(rows) + "\n"})
    assert "[CHANGED CLASS] Big: 1 new field, 9 new methods" in out
    assert "+ (and 5 more)" in out


def test_findings_caps_the_strings_listed_per_class(tmp_path):
    """The same cap as the members: a class that started loading a dozen
    literals is a fact, not twelve lines of one."""
    lits = ['"message number %d here"' % i for i in range(6)]
    out = findings(tmp_path, work={
        "appns.txt": "com/x/app\n",
        "classes3.dex.api": "com/x/app/Talker\t+\t.method public say()V\n",
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": "\n".join(lits) + "\n",
        "classes3.dex.owner": "".join("%s\tcom/x/app/Talker\n" % l for l in lits),
    })
    assert "[CHANGED CLASS] Talker: 1 new method, 6 new strings" in out
    assert "loads (and 2 more)" in out


def test_findings_attributes_a_class_change_to_the_app_not_its_dependencies(tmp_path):
    """A dependency's churn is not this release's story, and the digest is the
    one section short enough that a single library could crowd it out."""
    api = "\n".join([
        "androidx/compose/Foo\t+\t.field private final x:Z",
        "androidx/compose/Foo\t+\t.method public y()V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "CHANGED CLASS" not in out


def test_findings_ignores_kotlin_data_class_plumbing(tmp_path):
    """componentN/copy/copy$default are written by the compiler, one set per
    data class, and they restate the fields NEW STATE already lists."""
    api = "\n".join([
        "com/x/Offer\t+\t.field private final name:Ljava/lang/String;",
        "com/x/Offer\t+\t.method public accept()V",
        "com/x/Offer\t+\t.method public final component1()Ljava/lang/String;",
        "com/x/Offer\t+\t.method public final component12()Z",
        "com/x/Offer\t+\t.method public final copy(Ljava/lang/String;)Lcom/x/Offer;",
        "com/x/Offer\t+\t.method public static synthetic copy$default(Lcom/x/Offer;)V",
    ]) + "\n"
    out = findings(tmp_path, work={"classes3.dex.api": api})
    assert "[NEW API SURFACE] classes3.dex: 1 new method(s)" in out
    assert "public Offer.accept()" in out
    assert "component1" not in out
    assert "component12" not in out
    assert "copy" not in out


def test_findings_reports_user_facing_text_and_jni_descriptors(tmp_path):
    old = '"Something old and long enough"\n'
    new = (
        '"Something old and long enough"\n'
        '"The connection to this call was lost."\n'
        '"callFailed()V"\n'
        '" failed; ending it"\n'
    )
    out = findings(
        tmp_path,
        work={
            "classes3.dex.str.old": old,
            "classes3.dex.str.new": new,
            # The upcall is only reported because the library asks for it by
            # name; without this the descriptor is just a shapely string.
            "new/lib/arm64-v8a/librust_bridge.so": "\0callFailed\0",
        },
    )
    assert "[NEW USER-FACING TEXT]" in out
    assert "The connection to this call was lost." in out
    assert "[NEW NATIVE CALLBACK]" in out
    assert "[NEW ERROR HANDLING] classes3.dex" in out


def test_findings_jni_descriptor_absent_from_the_library_is_not_a_callback(tmp_path):
    """A private Kotlin suspend function has the shape of a JNI descriptor.

    computePinnedChats is the real case: build 20002240 added it as an ordinary
    method and it was reported as a new native callback. The library does not
    name it, so it is not one.
    """
    out = findings(
        tmp_path,
        work={
            "classes3.dex.str.old": "",
            "classes3.dex.str.new":
                '"computePinnedChats(Lkotlin/coroutines/Continuation;)'
                'Ljava/lang/Object;"\n',
            "new/lib/arm64-v8a/librust_bridge.so": "\0somethingElse\0",
        },
    )
    assert "[NEW NATIVE CALLBACK]" not in out


def test_findings_jni_reads_each_library_once_and_skips_unreadable_ones(tmp_path):
    """Two candidates: the library set is read on the first and reused on the
    second. An entry that cannot be opened is skipped, not fatal."""
    w = tmp_path / "work"
    (w / "new" / "lib" / "arm64-v8a").mkdir(parents=True)
    # A directory where a library is expected: open() raises, glob still lists it.
    (w / "new" / "lib" / "arm64-v8a" / "aaa-broken.so").mkdir()
    (w / "new" / "lib" / "arm64-v8a" / "librust_bridge.so").write_text(
        "\0callFailed\0callEnded\0"
    )
    out = findings(
        tmp_path,
        work={
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '"callFailed()V"\n"callEnded()V"\n',
        },
    )
    assert "[NEW NATIVE CALLBACK]" in out
    assert "callFailed()V" in out and "callEnded()V" in out


def test_findings_jni_descriptor_with_no_native_library_at_all(tmp_path):
    """No .so changed, so no library can have gained an upcall."""
    out = findings(
        tmp_path,
        work={
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '"callFailed()V"\n',
        },
    )
    assert "[NEW NATIVE CALLBACK]" not in out


def test_findings_ignores_generated_symbol_names_in_the_string_pool(tmp_path):
    """R8/Compose put their own generated names in the string pool.

    FailedStatusTag$lambda$372 matched the failure-path regex on the word
    "Failed" and was reported as new error handling. The real entry beside it
    must survive the filter.
    """
    out = findings(
        tmp_path,
        work={
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": (
                '"FailedStatusTag$lambda$372"\n'
                '"access$FailedStatusTag$lambda$373"\n'
                '"ComposableSingletons$ChatListScreenKt"\n'
                '"share of message text failed: "\n'
            ),
        },
    )
    assert "[NEW ERROR HANDLING] classes3.dex: 1 new" in out
    assert "share of message text failed: " in out
    assert "FailedStatusTag" not in out
    assert "ComposableSingletons" not in out


def test_findings_correlates_across_native_and_java(tmp_path):
    out = findings(
        tmp_path,
        work={
            "native.added.txt": "could not tell the service the call failed\n",
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '" failed; the alert closes this window, not the service"\n',
        },
    )
    assert "[CROSS-LAYER CHANGE]" in out


def test_findings_ignores_short_failure_strings(tmp_path):
    out = findings(
        tmp_path,
        work={"classes3.dex.str.old": "", "classes3.dex.str.new": '"failed"\n'},
    )
    assert "NEW ERROR HANDLING" not in out


def test_findings_ignores_class_descriptor_strings(tmp_path):
    out = findings(
        tmp_path,
        work={
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '"Lcom/example/Thing;"\n"\\u0000metadata"\n"file.kt:12"\n',
        },
    )
    assert "USER-FACING" not in out


def test_findings_tolerates_an_unreadable_work_file(tmp_path):
    w = tmp_path / "work"
    r = tmp_path / "report"
    w.mkdir()
    r.mkdir()
    # A directory where a file is expected must not crash the stage.
    (w / "native.added.txt").mkdir()
    out = run(FINDINGS, str(w), str(r)).stdout
    assert "No heuristic findings" in out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------- edge paths
#
# These exercise branches the happy-path tests above never reach: loop exits,
# skip-conditions, and the string-trimming rules that only fire on the kind of
# packed-blob fragments a real .rodata section produces.

def test_native_trims_a_camel_spliced_leading_token(tmp_path):
    # "adGot" begins partway through the previous literal and must be dropped,
    # leaving a phrase too short to report.
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so", b"\x00adGot empty sample here\x00")
    out = run(NATIVE, old, new).stdout
    assert reported(out) == ["empty sample here"]


def test_native_stops_at_an_overlong_word(tmp_path):
    long_word = b"x" * 19
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so", b"\x00the quick brown " + long_word + b" tail\x00")
    out = run(NATIVE, old, new).stdout
    assert reported(out) == ["the quick brown"]


def test_native_cuts_at_punctuation_glued_to_the_next_literal(tmp_path):
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so", b"\x00got empty sample!Packet too small\x00")
    out = run(NATIVE, old, new).stdout
    assert reported(out) == ["got empty sample!"]


def test_findings_manifest_lookup_skips_unrelated_lines(tmp_path):
    # The first '-' line carries no versionCode, so the search must keep going.
    out = findings(
        tmp_path,
        report={
            "2-manifest.txt": "-/m  something=else\n"
            "-/m  android:versionCode=1\n"
            "+/m  android:versionCode=2\n"
        },
    )
    assert "[VERSION] versionCode 1 -> 2" in out


def test_findings_manifest_with_no_version_at_all(tmp_path):
    out = findings(tmp_path, report={"2-manifest.txt": "+/m/uses-permission  android:name=NFC\n"})
    assert "VERSION" not in out
    assert "[PERMISSION CHANGE] the app requests 1 permission(s)" in out
    assert "NFC" in out


def test_findings_reports_a_dropped_permission(tmp_path):
    out = findings(tmp_path, report={"2-manifest.txt": "-/m/uses-permission  android:name=SMS\n"})
    assert "[PERMISSION CHANGE] the app no longer requests 1 permission(s)" in out
    assert "SMS" in out


def test_findings_names_new_and_removed_components(tmp_path):
    out = findings(
        tmp_path,
        report={"2-manifest.txt":
                "+/manifest/application/service[com.x.CallService]  "
                "android:name=com.x.CallService\n"
                "-/manifest/application/receiver[com.x.BootReceiver]  "
                "android:name=com.x.BootReceiver\n"},
    )
    assert "[NEW COMPONENT] the app declares 1 component(s)" in out
    assert "service        com.x.CallService" in out
    assert "[REMOVED COMPONENT] the app no longer declares 1 component(s)" in out
    assert "receiver       com.x.BootReceiver" in out
    # Already reported in a legible shape; not repeated as a raw diff line.
    assert "MANIFEST CHANGE" not in out


def test_findings_names_newly_claimed_urls(tmp_path):
    out = findings(
        tmp_path,
        report={"2-manifest.txt":
                "+/manifest/application/activity[com.x.Main]/intent-filter[VIEW]"
                "/data[android:host=www.example.com]  "
                "android:host=www.example.com\n"},
    )
    assert "[NEW INTENT FILTER] the app claims 1 new URL pattern(s)" in out
    assert "host www.example.com" in out
    assert "MANIFEST CHANGE" not in out


def test_findings_still_reports_manifest_lines_no_detector_claims(tmp_path):
    out = findings(
        tmp_path,
        report={"2-manifest.txt": "+/manifest/uses-sdk  android:targetSdkVersion=35\n"},
    )
    assert "[MANIFEST CHANGE]" in out


def test_findings_reports_native_exports_jni_entry_points_first(tmp_path):
    out = findings(
        tmp_path,
        work={"sym.old": "_ZN4rust3oldEv\nkept\n",
              "sym.new": "kept\n_ZN4rust3newEv\nJava_com_x_Bridge_ring\n"},
    )
    assert "[NEW NATIVE EXPORT] native: 2 symbol(s) exports" in out
    added = out.split("[NEW NATIVE EXPORT]")[1].splitlines()[1:3]
    assert added[0].strip() == "Java_com_x_Bridge_ring", added
    assert "[REMOVED NATIVE EXPORT] native: 1 symbol(s) no longer exports" in out
    assert "_ZN4rust3oldEv" in out


def test_findings_reports_entries_no_other_stage_read(tmp_path):
    out = findings(
        tmp_path,
        work={"changed.txt": "assets/model.tflite\n", "added.txt": "res/raw/tone.ogg\n"},
    )
    assert "[OTHER FILES] 1 non-code file(s) changed" in out
    assert "assets/model.tflite" in out
    assert "res/raw/tone.ogg" in out


def test_findings_ignores_entries_another_stage_owns_or_the_build_rewrites(tmp_path):
    """dex and .so have their own stages; the signature blocks and the baseline
    profile move whenever anything else does and say nothing themselves."""
    out = findings(
        tmp_path,
        work={"changed.txt": "classes3.dex\nlib/arm64-v8a/libx.so\n"
                             "AndroidManifest.xml\nassets/dexopt/baseline.prof\n"
                             "META-INF/MANIFEST.MF\n"},
    )
    assert "OTHER FILES" not in out


def test_findings_pairs_a_reworded_message_with_the_one_it_replaced(tmp_path):
    out = findings(
        tmp_path,
        work={"classes3.dex.str.old": '"Failed to get live view"\n',
              "classes3.dex.str.new": '"Failed to get live view for guid="\n'},
    )
    assert "[REWORDED TEXT] classes3.dex: 1 message(s)" in out
    assert 'was "Failed to get live view"' in out
    assert 'now "Failed to get live view for guid="' in out
    # Counted once, as the one edit it is.
    assert "NEW USER-FACING TEXT" not in out
    assert "REMOVED USER-FACING TEXT" not in out


def test_findings_reports_a_message_that_simply_disappeared(tmp_path):
    out = findings(
        tmp_path,
        work={"classes3.dex.str.old": '"Tap here to retry the upload"\n',
              "classes3.dex.str.new": ""},
    )
    assert "[REMOVED USER-FACING TEXT] classes3.dex: 1 message(s)" in out
    assert "Tap here to retry the upload" in out


def test_findings_skips_removed_strings_that_are_not_stubs(tmp_path):
    out = findings(
        tmp_path,
        work={
            "native.removed.txt": "an ordinary retired message\nTODO the real stub\n",
            "native.added.txt": "unrelated new text\n",
        },
    )
    assert out.count("[STUB IMPLEMENTED]") == 1
    assert "TODO the real stub" in out


def test_findings_cross_layer_ignores_phrases_with_too_few_tokens(tmp_path):
    # Every token here is a stop word, so there is nothing distinctive to
    # correlate on and the phrase must be skipped rather than matched loosely.
    out = findings(
        tmp_path,
        work={
            "native.added.txt": "this that with from\n",
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '"this that with from and more"\n',
        },
    )
    assert "CROSS-LAYER" not in out


def test_findings_cross_layer_reports_each_native_phrase_once(tmp_path):
    # One native phrase matching two Java strings must yield a single entry.
    out = findings(
        tmp_path,
        work={
            "native.added.txt": "could not reach the service endpoint\n",
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '"service endpoint unreachable here"\n'
                                    '"another service endpoint problem"\n',
        },
    )
    assert out.count("[CROSS-LAYER CHANGE]") == 1
    assert "1 concept(s)" in out


def test_findings_cross_layer_with_no_java_match(tmp_path):
    out = findings(
        tmp_path,
        work={
            "native.added.txt": "could not reach the service endpoint\n",
            "classes3.dex.str.old": "",
            "classes3.dex.str.new": '"completely unrelated wording"\n',
        },
    )
    assert "CROSS-LAYER" not in out


def test_findings_cross_layer_names_the_same_java_string_on_every_run(tmp_path):
    """Only the first matching Java string is reported, so which one that is
    must not depend on set iteration order — which varies with the hash seed."""
    work = {
        "native.added.txt": "could not reach the service endpoint\n",
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": "".join(
            '"service endpoint number %d failed"\n' % i for i in range(50)
        ),
    }
    seen = set()
    for seed in ("0", "1", "12345"):
        w = tmp_path / seed / "work"
        r = tmp_path / seed / "report"
        w.mkdir(parents=True)
        r.mkdir(parents=True)
        for name, text in work.items():
            (w / name).write_text(text)
        env = dict(os.environ, PYTHONHASHSEED=seed)
        p = subprocess.run(
            [sys.executable, FINDINGS, str(w), str(r)],
            capture_output=True, text=True, env=env,
        )
        assert p.returncode == 0, p.stderr
        seen.add(p.stdout)
    assert len(seen) == 1, "the pairing differs between runs"


def test_findings_skips_a_native_library_it_cannot_map(tmp_path):
    """An empty .so cannot be mapped and cannot contain anything; the scan
    steps over it instead of failing the stage."""
    w = tmp_path / "work"
    (w / "new" / "lib" / "arm64-v8a").mkdir(parents=True)
    (w / "new" / "lib" / "arm64-v8a" / "libempty.so").write_bytes(b"")
    (w / "new" / "lib" / "arm64-v8a" / "libreal.so").write_bytes(b"\0callFailed\0")
    out = findings(
        tmp_path,
        work={"classes3.dex.str.old": "", "classes3.dex.str.new": '"callFailed()V"\n'},
    )
    assert "[NEW NATIVE CALLBACK]" in out


def test_token_index_needs_two_shared_words(tmp_path):
    ns = runpy.run_path(FINDINGS, run_name="not_main")
    index = ns["TokenIndex"](
        ["reconnecting to the peer failed", "unrelated wording entirely"],
        ns["concepts"],
    )
    assert index.matches(ns["concepts"]("peer reconnecting gave up")) == [
        "reconnecting to the peer failed"
    ]
    # One shared word is not evidence of anything.
    assert index.matches(ns["concepts"]("peer is fine")) == []


def test_evidence_reads_each_artefact_once(tmp_path):
    """Three detectors want the string pools, and a real pool is megabytes."""
    ns = runpy.run_path(FINDINGS, run_name="not_main")
    w = tmp_path / "work"
    w.mkdir()
    (w / "classes3.dex.str.new").write_text('"hello there"\n')
    e = ns["Evidence"](str(w), str(tmp_path))
    path = str(w / "classes3.dex.str.new")
    assert e.lines(path) == ['"hello there"']
    os.remove(path)
    assert e.lines(path) == ['"hello there"'], "the second read came from disk"


def test_findings_api_ignores_malformed_rows(tmp_path):
    out = findings(tmp_path, work={"classes3.dex.api": "not-a-tab-separated-row\n"})
    assert "NEW API SURFACE" not in out


def test_findings_api_with_only_removals(tmp_path):
    out = findings(tmp_path, work={"classes3.dex.api": "com/x/Real\t-\t.method public gone()V\n"})
    assert "NEW API SURFACE" not in out
    assert "NEW STATE" not in out


def test_findings_api_class_whose_last_addition_is_a_field(tmp_path):
    out = findings(
        tmp_path,
        work={"classes3.dex.api": "com/y/Other\t+\t.field private a:I\n"
                                  "com/y/Other\t+\t.field private b:Z\n"},
    )
    assert "[NEW STATE] classes3.dex: 2 new field(s)" in out


def test_native_verify_checks_every_spilled_row(tmp_path):
    same = write(tmp_path, "x.so", b"\x00identical content here\x00")
    prefix = str(tmp_path / "spill")
    # First row is genuinely present; the loop must carry on to the second.
    open(prefix + ".added.txt", "w").write("identical content here\nabsent phrase\n")
    open(prefix + ".removed.txt", "w").write("")
    out = run(NATIVE, "--verify", same, same, prefix).stdout
    # The first row is present in both files, so it is itself a mismatch for
    # "added"; the second is absent from both. Two rows, two verdicts.
    assert out.count("MISMATCH (added)") == 2


def test_native_skips_a_rejected_candidate_and_keeps_a_later_one(tmp_path):
    # Two candidates in one printable run: the first has no spaces and must be
    # discarded, the second is a real message.
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(
        tmp_path, "new.so",
        b"\x00AAAAAAAAAAAAAAA# could not tell the service it failed\x00",
    )
    assert reported(run(NATIVE, old, new).stdout) == [
        "could not tell the service it failed"
    ]


def test_findings_api_ignores_entries_that_are_neither_method_nor_field(tmp_path):
    out = findings(
        tmp_path,
        work={"classes3.dex.api": "com/z/Q\t+\t.annotation runtime Foo\n"
                                  "com/z/Q\t+\t.method public real()V\n"},
    )
    assert "[NEW API SURFACE] classes3.dex: 1 new method(s)" in out
    assert "annotation" not in out


def test_native_verify_passes_over_rows_that_check_out(tmp_path):
    old = write(tmp_path, "old.so", b"\x00common preamble text\x00")
    new = write(
        tmp_path, "new.so",
        b"\x00common preamble text\x00brand new message here\x00"
        b"another new message now\x00",
    )
    prefix = str(tmp_path / "spill")
    open(prefix + ".added.txt", "w").write(
        "brand new message here\nanother new message now\n"
    )
    open(prefix + ".removed.txt", "w").write("")
    out = run(NATIVE, "--verify", old, new, prefix).stdout
    assert "MISMATCH" not in out
    assert "verified against primary ABI: OK" in out


# ---------------------------------------------------------------- version

VERSION = os.path.join(ROOT, "lib", "version.py")


def apk_with(manifest, extra=None, path=None):
    z = zipfile.ZipFile(path, "w")
    z.writestr("AndroidManifest.xml", manifest)
    for name, text in (extra or {}).items():
        z.writestr(name, text)
    z.close()
    return str(path)


VERSIONED = {
    "package": (0x03, 0, "com.example.app"),
    "versionCode": (0x10, 20002242, None),
    "versionName": (0x03, 0, "2.0.0"),
}


def test_version_reads_an_apk_manifest(tmp_path):
    p = apk_with(axml(VERSIONED), path=tmp_path / "whatever.bin")
    out = run(VERSION, p)
    assert out.returncode == 0
    assert out.stdout.strip().split("\t") == [
        "apk", "com.example.app", "20002242", "2.0.0"]


def test_version_reads_a_utf8_string_pool(tmp_path):
    p = apk_with(axml(VERSIONED, utf8=True), path=tmp_path / "u.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[2] == "20002242"


def test_version_reads_a_hex_encoded_version(tmp_path):
    p = apk_with(axml({"versionCode": (0x11, 255, None)}), path=tmp_path / "h.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[2] == "255"  # not 0xff


def test_version_falls_back_to_the_raw_value_for_other_types(tmp_path):
    # A dimension: not a string, not an int and not a reference, but the raw
    # string index still names it.
    p = apk_with(axml({"versionName": (0x05, 0, "1.2.3")}), path=tmp_path / "r.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[3] == "1.2.3"


def test_version_skips_elements_before_the_manifest(tmp_path):
    """A leading element that is not <manifest> must not be mistaken for it."""
    a = axml({"versionCode": (0x10, 7, None)}, element="something-else")
    b = axml(VERSIONED)
    # Splice the two documents' element chunks: pool from b, both elements.
    p = apk_with(b[:8] + b[8:] , path=tmp_path / "ok.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[2] == "20002242"
    p2 = apk_with(a, path=tmp_path / "other.apk")
    out = run(VERSION, p2, expect_ok=False)
    assert out.returncode == 1
    assert "not <manifest>" in out.stderr


def test_version_detects_a_bundle_and_uses_bundletool(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "bundletool"
    fake.write_text(
        "#!/bin/sh\n"
        # Real bundletool declares the namespace; without it the document is
        # not well-formed XML and ElementTree rejects it.
        'echo \'<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
        ' package="com.example.app" android:versionCode="20002242"'
        ' android:versionName="2.0.0"/>\'\n'
    )
    fake.chmod(0o755)
    p = tmp_path / "b.aab"
    z = zipfile.ZipFile(p, "w")
    z.writestr("BundleConfig.pb", "x")
    z.writestr("base/manifest/AndroidManifest.xml", "x")
    z.close()
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    out = subprocess.run([sys.executable, VERSION, str(p)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0
    assert out.stdout.strip().split("\t") == [
        "aab", "com.example.app", "20002242", "2.0.0"]


def test_version_reports_a_bundle_it_cannot_read(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "bundletool"
    fake.write_text("#!/bin/sh\necho ''\n")
    fake.chmod(0o755)
    p = tmp_path / "b.aab"
    z = zipfile.ZipFile(p, "w")
    z.writestr("BundleConfig.pb", "x")
    z.close()
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])
    out = subprocess.run([sys.executable, VERSION, str(p)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 1
    assert "printed no manifest" in out.stderr


def test_version_reports_a_missing_bundletool(tmp_path):
    p = tmp_path / "b.aab"
    z = zipfile.ZipFile(p, "w")
    z.writestr("BundleConfig.pb", "x")
    z.close()
    out = subprocess.run([sys.executable, VERSION, str(p)],
                         capture_output=True, text=True,
                         env=dict(os.environ, PATH=str(tmp_path)))
    assert out.returncode == 1
    assert "bundletool did not run" in out.stderr


def test_version_rejects_an_archive_with_no_manifest(tmp_path):
    p = tmp_path / "x.apk"
    zipfile.ZipFile(p, "w").close()
    out = run(VERSION, str(p), expect_ok=False)
    assert out.returncode == 1
    assert "no AndroidManifest.xml" in out.stderr


def test_version_rejects_a_manifest_that_is_not_axml(tmp_path):
    p = apk_with(b"<?xml version='1.0'?><manifest/>", path=tmp_path / "t.apk")
    out = run(VERSION, str(p), expect_ok=False)
    assert out.returncode == 1
    assert "not a binary XML chunk" in out.stderr


def test_version_rejects_a_chunk_that_runs_past_the_end(tmp_path):
    good = axml(VERSIONED)
    bad = good[:12] + struct.pack("<I", 1 << 20) + good[16:]
    out = run(VERSION, apk_with(bad, path=tmp_path / "o.apk"), expect_ok=False)
    assert out.returncode == 1
    assert "past the end" in out.stderr


def test_version_rejects_an_element_before_the_string_pool(tmp_path):
    good = axml(VERSIONED)
    pool_size = struct.unpack_from("<I", good, 12)[0]
    elem = good[8 + pool_size:]
    out = run(VERSION, apk_with(good[:8] + elem, path=tmp_path / "e.apk"), expect_ok=False)
    assert out.returncode == 1
    assert "before the string pool" in out.stderr


def test_version_rejects_a_file_that_is_not_a_zip(tmp_path):
    p = tmp_path / "nope.apk"
    p.write_bytes(b"not a zip at all")
    assert run(VERSION, str(p), expect_ok=False).returncode == 1


def test_version_needs_exactly_one_argument():
    out = run(VERSION, expect_ok=False)
    assert out.returncode == 2
    assert "usage" in out.stderr


def test_pretty_renders_descriptors_as_a_signature():
    ns = runpy.run_path(FINDINGS, run_name="not_main")
    pretty = ns["pretty"]
    assert pretty(
        ".method private static final AudioCall(Lcom/x/CallActivity;"
        "Ljava/lang/String;[ILandroidx/compose/runtime/Composer;I)V",
        "com/x/CallScreenKt",
    ) == ("private static final CallScreenKt.AudioCall("
          "CallActivity, String, int[], Composer, int)")
    # A non-void return is stated; void is the default and stays unsaid.
    assert pretty(".method public run()I", "com/x/Job") == "public Job.run() -> int"
    # A nested class keeps the outer name, in the form source uses.
    assert pretty(".method public f(Lcom/x/BalloonContent$App;)V") == \
        "public f(BalloonContent.App)"
    # Fields.
    assert pretty(".field private final flag:Z", "com/x/T") == \
        "private final T.flag: boolean"


def test_pretty_leaves_alone_what_it_cannot_parse():
    """The api artefact is read from disk; a line that is not a declaration
    must come through unchanged rather than half-rendered."""
    ns = runpy.run_path(FINDINGS, run_name="not_main")
    pretty = ns["pretty"]
    assert pretty(".method broken") == "broken"
    assert pretty(".field noColon") == ".field noColon"
    assert pretty(".method public <init>()V", "com/x/T") == "public T.<init>()"


# ------------------------------------------------- app vs bundled libraries

APP_AND_LIB = (
    "com/acme/app/Thing\t+\t.method public fixIt()V\n"
    "com/acme/app/Thing\t+\t.field private tries:I\n"
    "org/jsoup/parser/Tag\t+\t.method public selfClose()V\n"
    "androidx/compose/Foo\t+\t.field private x:I\n"
)


def test_findings_separates_app_code_from_bundled_libraries(tmp_path):
    out = findings(tmp_path, work={"appns.txt": "com/acme/app\n",
                                   "dex.api": APP_AND_LIB})
    assert "1 new method(s) in com/acme/app" in out
    assert "public Thing.fixIt()" in out
    assert "Tag.selfClose()" not in out
    assert "(+1 more in bundled libraries" in out
    assert "1 new field(s) in com/acme/app" in out
    assert "private Thing.tries: int" in out


def test_findings_reports_everything_when_the_namespace_is_unknown(tmp_path):
    """No namespace is not a reason to hide a dependency's change."""
    out = findings(tmp_path, work={"dex.api": APP_AND_LIB})
    assert "2 new method(s) in the build" in out
    assert "public Tag.selfClose()" in out
    assert "bundled libraries" not in out


def test_findings_omits_a_group_that_is_entirely_empty(tmp_path):
    out = findings(tmp_path, work={"appns.txt": "com/acme/app\n",
                                   "dex.api": "com/acme/app/T\t-\t.method public g()V\n"})
    assert "NEW API SURFACE" not in out
    assert "NEW STATE" not in out


# ------------------------------------------------------- body-only changes

def test_findings_reports_methods_rewritten_behind_an_unchanged_signature(tmp_path):
    out = findings(
        tmp_path,
        work={
            "appns.txt": "com/acme/app\n",
            "dex.api": (
                "com/acme/app/Quiet\t~\t.method public tick()V\n"
                "com/acme/app/Quiet\t~\t.method public tock()V\n"
                "com/acme/app/Loud\t+\t.method public added()V\n"
                "com/acme/app/Quiet$$ExternalSyntheticLambda3\t~\t.method public run()V\n"
                "androidx/compose/Foo\t~\t.method public draw()V\n"
            ),
        },
    )
    assert "[BEHAVIOUR CHANGE] dex: 2 method(s) rewritten on 1 class(es)" in out
    body = out.split("[BEHAVIOUR CHANGE]")[1]
    assert "public Quiet.tick()" in body
    assert "public Quiet.tock()" in body
    assert "ExternalSyntheticLambda" not in out
    assert "androidx/compose/Foo" not in out
    assert "(+1 more in bundled libraries" in body


def test_findings_rewritten_methods_lead_with_the_busiest_class(tmp_path):
    out = findings(
        tmp_path,
        work={
            "dex.api": (
                "a/Quiet\t~\t.method public one()V\n"
                "a/Busy\t~\t.method public x()V\n"
                "a/Busy\t~\t.method public y()V\n"
            ),
        },
    )
    body = out.split("[BEHAVIOUR CHANGE]")[1]
    assert body.index("Busy") < body.index("Quiet")


def test_findings_rewritten_methods_show_at_most_three_per_class(tmp_path):
    rows = "".join("a/B\t~\t.method public m%d()V\n" % i for i in range(6))
    out = findings(tmp_path, work={"dex.api": rows})
    body = out.split("[BEHAVIOUR CHANGE]")[1]
    assert "6 method(s) rewritten on 1 class(es)" in out
    assert body.count("public B.m") == 3


def test_findings_no_rewritten_methods_reports_nothing(tmp_path):
    out = findings(tmp_path, work={"dex.norm": "NEW\ta/B\n"})
    assert "BEHAVIOUR CHANGE" not in out


def test_version_reads_a_long_utf16_pool_entry(tmp_path):
    """Above 0x7FFF characters the UTF-16 length takes two units."""
    p = apk_with(axml(VERSIONED, filler="x" * 0x8001), path=tmp_path / "l.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[2] == "20002242"


def test_version_reads_a_long_utf8_pool_entry(tmp_path):
    """Above 127 bytes the UTF-8 length takes two bytes."""
    p = apk_with(axml(VERSIONED, utf8=True, filler="y" * 200),
                 path=tmp_path / "l8.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[1] == "com.example.app"


def test_version_walks_past_chunks_it_does_not_care_about(tmp_path):
    """A resource-map chunk sits between the pool and the first element."""
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + 8) + struct.pack("<II", 1, 2)
    p = apk_with(axml(VERSIONED, extra_chunk=resmap), path=tmp_path / "rm.apk")
    assert run(VERSION, p).stdout.strip().split("\t")[2] == "20002242"


def test_version_renders_an_unrecognised_type_as_its_raw_value(tmp_path):
    """A dimension has no textual form here, but its bytes still diff."""
    p = apk_with(axml({"versionCode": (0x05, 1, None)}), path=tmp_path / "d.apk")
    out = run(VERSION, p)
    assert out.stdout.rstrip("\n").split("\t") == ["apk", "", "0x00000001", ""]


# ---------------------------------------------------------------- axml

AXMLPY = os.path.join(ROOT, "lib", "axml.py")


def flat(path):
    return run(MANIFEST, str(path)).stdout.splitlines()


def test_manifest_reads_an_apk_directly(tmp_path):
    """No apktool: the binary manifest is parsed here, versions included."""
    p = apk_with(axml(VERSIONED), path=tmp_path / "a.apk")
    lines = flat(p)
    assert "/manifest  versionCode=20002242" in lines
    assert "/manifest  package=com.example.app" in lines


def test_manifest_renders_every_value_type(tmp_path):
    doc = axml({
        "a": (0x12, 1, None),        # boolean true
        "b": (0x12, 0, None),        # boolean false
        "c": (0x01, 0x7f110020, None),   # reference
        "d": (0x04, 0x3f800000, None),   # float 1.0
        "e": (0x1c, 0xff112233, None),   # colour: no textual form here
    })
    lines = flat(apk_with(doc, path=tmp_path / "t.apk"))
    assert "/manifest  a=true" in lines
    assert "/manifest  b=false" in lines
    assert "/manifest  c=@0x7f110020" in lines
    assert "/manifest  d=1.0" in lines
    assert "/manifest  e=0xff112233" in lines


def test_manifest_keeps_namespaced_attributes_apart(tmp_path):
    """Two attributes of the same local name in different namespaces."""
    import sys as _s
    _s.path.insert(0, os.path.join(ROOT, "lib"))
    import axml as ax
    doc = axml({"name": (0x03, 0, "plain")})
    root = ax.parse(doc)
    assert root.attrib == {"name": "plain"}


def test_axml_rejects_a_document_with_no_elements(tmp_path):
    good = axml(VERSIONED)
    pool_size = struct.unpack_from("<I", good, 12)[0]
    only_pool = good[:8] + good[8:8 + pool_size]
    out = run(MANIFEST, apk_with(only_pool, path=tmp_path / "n.apk"),
              expect_ok=False)
    assert out.returncode == 1
    assert "no elements" in out.stderr


def test_axml_rejects_an_element_closed_but_never_opened(tmp_path):
    good = axml(VERSIONED)
    end = struct.pack("<HHIII", 0x0103, 16, 16, 1, 0xFFFFFFFF)
    pool_size = struct.unpack_from("<I", good, 12)[0]
    doc = good[:8] + good[8:8 + pool_size] + end
    doc = struct.pack("<HHI", 0x0003, 8, len(doc)) + doc[8:]
    out = run(MANIFEST, apk_with(doc, path=tmp_path / "c.apk"), expect_ok=False)
    assert out.returncode == 1
    assert "never opened" in out.stderr


def test_axml_rejects_a_string_index_past_the_pool(tmp_path):
    good = axml({"versionCode": (0x10, 1, None)})
    # The document ends with a 24-byte END_ELEMENT chunk, preceded by the
    # root's single 20-byte attribute record; its name index is the second word.
    at = len(good) - 24 - 20 + 4
    bad = good[:at] + struct.pack("<I", 999) + good[at + 4:]
    out = run(MANIFEST, apk_with(bad, path=tmp_path / "p.apk"), expect_ok=False)
    assert out.returncode == 1
    assert "past the end of the pool" in out.stderr


def test_manifest_nests_children_and_declares_namespaces(tmp_path):
    """A real manifest: namespace chunk, nested elements, repeated siblings."""
    import sys as _s
    _s.path.insert(0, os.path.join(ROOT, "lib"))
    import axml as ax
    xml = (
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
        'package="com.example.app">'
        '<uses-permission android:name="android.permission.CAMERA"/>'
        '<application android:name="com.example.app.App">'
        '<activity android:name="com.example.app.Main"/>'
        '</application></manifest>'
    )
    (tmp_path / "m.xml").write_text(xml)
    lines = flat(tmp_path / "m.xml")
    assert "/manifest/application  android:name=com.example.app.App" in lines
    assert any("uses-permission  android:name=android.permission.CAMERA" in l
               for l in lines)
    assert any("/activity  android:name=com.example.app.Main" in l for l in lines)
    assert ax.ANDROID_NS.endswith("res/android")


def test_manifest_usage_is_reported(tmp_path):
    assert run(MANIFEST, expect_ok=False).returncode == 2
    assert run(MANIFEST, "a", "b", "c", "d", expect_ok=False).returncode == 2
    assert run(MANIFEST, "--diff", "only-one", expect_ok=False).returncode == 2


def test_manifest_reports_an_archive_with_no_manifest(tmp_path):
    p = tmp_path / "empty.apk"
    zipfile.ZipFile(p, "w").close()
    out = run(MANIFEST, str(p), expect_ok=False)
    assert out.returncode == 1
    assert "no AndroidManifest.xml" in out.stderr


def test_manifest_reports_unreadable_input(tmp_path):
    p = tmp_path / "junk.xml"
    p.write_text("<not-closed>")
    out = run(MANIFEST, str(p), expect_ok=False)
    assert out.returncode == 1
    assert "cannot read" in out.stderr


def bundle_with(tmp_path, script):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "bundletool"
    fake.write_text(script)
    fake.chmod(0o755)
    p = tmp_path / "b.aab"
    z = zipfile.ZipFile(p, "w")
    z.writestr("BundleConfig.pb", "x")
    z.close()
    return p, dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"])


def test_manifest_flattens_a_bundle_through_bundletool(tmp_path):
    p, env = bundle_with(tmp_path, "#!/bin/sh\necho '<manifest package=\"p\"/>'\n")
    out = subprocess.run([sys.executable, MANIFEST, str(p)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0
    assert "/manifest  package=p" in out.stdout


def test_manifest_reports_a_bundle_it_cannot_read(tmp_path):
    p, env = bundle_with(tmp_path, "#!/bin/sh\necho ''\n")
    out = subprocess.run([sys.executable, MANIFEST, str(p)],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 1
    assert "printed no manifest" in out.stderr


def test_manifest_reports_a_missing_bundletool(tmp_path):
    p = tmp_path / "b.aab"
    z = zipfile.ZipFile(p, "w")
    z.writestr("BundleConfig.pb", "x")
    z.close()
    out = subprocess.run([sys.executable, MANIFEST, str(p)], capture_output=True,
                         text=True, env=dict(os.environ, PATH=str(tmp_path)))
    assert out.returncode == 1
    assert "bundletool did not run" in out.stderr


def test_manifest_diff_reconciles_every_interchangeable_encoding(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("/m  x=0x5b0\n/m  y=@0x7f110020\n/m  z=2\n/m  w=@a/b\n")
    b.write_text("/m  x=0x000005b0\n/m  y=@string/app_name\n/m  z=singleTask\n"
                 "/m  w=@a/c\n")
    out = run(MANIFEST, "--diff", str(a), str(b)).stdout
    assert "x=" not in out, "0x5b0 and 0x000005b0 are one value"
    assert "y=" not in out, "an id and a name cannot be compared"
    assert "z=" not in out, "an int and a symbol cannot be compared"
    assert "w=@a/b" in out and "w=@a/c" in out, "two names that differ do differ"


def test_manifest_reads_namespaces_and_nested_elements(tmp_path):
    """A binary manifest with a namespace chunk and real children."""
    doc = axml(
        {"package": (0x03, 0, "com.example.app"),
         "android:versionCode": (0x10, 7, None)},
        children=[("uses-permission", {"android:name": (0x03, 0, "CAMERA")}),
                  ("application", {"android:name": (0x03, 0, "com.example.App")})],
    )
    lines = flat(apk_with(doc, path=tmp_path / "n.apk"))
    assert "/manifest  android:versionCode=7" in lines
    assert "/manifest  package=com.example.app" in lines
    assert "/manifest/uses-permission  android:name=CAMERA" in lines
    assert ("/manifest/application  "
            "android:name=com.example.App") in lines


def test_manifest_keeps_the_first_of_two_root_elements(tmp_path):
    """A malformed document with two roots must not lose the first."""
    doc = axml({"package": (0x03, 0, "com.example.app")},
               children=[("extra", {})])
    second = axml({"package": (0x03, 0, "other")})
    pool_size = struct.unpack_from("<I", second, 12)[0]
    doc = doc + second[8 + pool_size:]
    doc = struct.pack("<HHI", 0x0003, 8, len(doc)) + doc[8:]
    lines = flat(apk_with(doc, path=tmp_path / "two.apk"))
    assert "/manifest  package=com.example.app" in lines


def test_findings_attributes_a_new_string_to_the_class_that_loads_it(tmp_path):
    out = findings(
        tmp_path,
        work={
            "appns.txt": "com/acme/app\n",
            "dex.str.old": "",
            "dex.str.new": '"The call could not be re-established."\n',
            "dex.owner": (
                '"The call could not be re-established."\t'
                'com/acme/app/ComposableSingletons$CallScreenKt$lambda-3$1\n'
                '"The call could not be re-established."\torg/jsoup/Ignored\n'
            ),
        },
    )
    # Named by the file it belongs to, not by the wrapper R8 generated, and
    # a bundled library's copy is not the app's.
    assert "[CallScreenKt]" in out
    assert "jsoup" not in out


def test_findings_reports_strings_and_log_lines_that_disappeared(tmp_path):
    out = findings(
        tmp_path,
        work={
            "native.removed.txt": "could not reach the relay\n",
            "dex.str.old": '"failed to open the database"\n"Hello there friend"\n',
            "dex.str.new": '"Hello there friend"\n',
        },
    )
    assert "[REMOVED ERROR HANDLING] native: 1" in out
    assert "could not reach the relay" in out
    assert "[REMOVED ERROR HANDLING] dex: 1" in out
    assert "failed to open the database" in out


def test_findings_usage_is_reported():
    out = run(FINDINGS, expect_ok=False)
    assert out.returncode == 2
    assert "usage" in out.stderr


def test_manifest_diff_ignores_a_bare_path_present_in_both(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("/m/queries\n/m  x=1\n")
    b.write_text("/m/queries\n/m  x=2\n")
    out = run(MANIFEST, "--diff", str(a), str(b)).stdout
    assert "queries" not in out
    assert "x=1" in out and "x=2" in out


# ---------------------------------------------------------------- locale
#
# The helpers report the app's own text, and an app's text is not ASCII: a
# curly quote in a user-facing string, an accent in an android:label. Python
# picks its default text encoding from the environment, so a run under a POSIX
# locale with the C-locale coercion turned off gets ASCII and aborts on the
# first such character. Which characters the tool can carry must be a property
# of the tool, not of whoever's shell it was started from.

ASCII_LOCALE = dict(
    os.environ,
    LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0",
)
ASCII_LOCALE.pop("PYTHONIOENCODING", None)

NON_ASCII = 'Impossible de rétablir l’appel — réessayez.'


def test_helpers_carry_non_ascii_text_under_an_ascii_locale(tmp_path):
    w = tmp_path / "work"
    r = tmp_path / "report"
    w.mkdir()
    r.mkdir()
    (w / "classes3.dex.str.old").write_text("", encoding="utf-8")
    (w / "classes3.dex.str.new").write_text('"%s"\n' % NON_ASCII, encoding="utf-8")

    p = subprocess.run(
        [sys.executable, FINDINGS, str(w), str(r)],
        capture_output=True, env=ASCII_LOCALE,
    )
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    written = (r / "7-findings.txt").read_text(encoding="utf-8")
    assert NON_ASCII in written, "the report must keep the text it is quoting"
    assert "NEW USER-FACING TEXT" in written
    # stdout is the same report; it may transliterate what the terminal cannot
    # take, but it must not abort.
    assert b"NEW USER-FACING TEXT" in p.stdout


def test_manifest_carries_non_ascii_values_under_an_ascii_locale(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("/m  android:label=Café\n", encoding="utf-8")
    b.write_text("/m  android:label=Cafétera\n", encoding="utf-8")
    p = subprocess.run(
        [sys.executable, MANIFEST, "--diff", str(a), str(b)],
        capture_output=True, env=ASCII_LOCALE,
    )
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert "Cafétera" in p.stdout.decode("utf-8")


def test_findings_says_when_bodies_were_not_compared(tmp_path):
    """An absent finding and a finding of absence read identically.

    The apktool fallback cannot hash an opcode sequence, so it emits no `~`
    rows and BEHAVIOUR CHANGE never fires. Without this the report is
    indistinguishable from one where every body genuinely held.
    """
    out = findings(tmp_path, work={"no-body-diff.txt": "apktool\n"})
    assert "[INCOMPLETE] method bodies were not compared" in out
    assert "cargo build --release" in out


def test_findings_stays_quiet_when_bodies_were_compared(tmp_path):
    out = findings(tmp_path, work={"classes3.dex.api": "a\t~\t.method x()V\n"})
    assert "INCOMPLETE" not in out


# ---------------------------------------------------------------- binary XML
#
# res/xml/, res/layout/ and the rest are the same AXML the manifest is, and the
# same decoder reads them. Before stage 2 pointed it at them, a FileProvider
# root moving out of the app's own files/ directory reached the report as the
# words "1 non-code file(s) changed".

def apk_with_paths(tmp_path, name, value, utf8=False):
    """An APK carrying one res/xml/file_paths.xml with the given path value."""
    xml = tmp_path / (name + ".xml")
    subprocess.run([sys.executable, os.path.join(HERE, "mkaxml.py"),
                    "--paths", str(xml), value] + (["--utf8"] if utf8 else []),
                   check=True)
    p = tmp_path / (name + ".apk")
    with zipfile.ZipFile(p, "w") as z:
        z.write(str(xml), "res/xml/file_paths.xml")
    return str(p)


def rows_file(tmp_path, *names):
    p = tmp_path / "rows.tsv"
    p.write_text("".join("%s\t%s\t%s\n" % (n, n, n) for n in names))
    return str(p)


def test_manifest_diffs_a_binary_xml_entry_of_two_archives(tmp_path):
    a = apk_with_paths(tmp_path, "a", "attachments/")
    b = apk_with_paths(tmp_path, "b", "../app_flutter/attachments/")
    out = run(MANIFEST, "--entries", a, b,
              rows_file(tmp_path, "res/xml/file_paths.xml")).stdout
    assert out.splitlines() == [
        "=== res/xml/file_paths.xml",
        "-/paths/files-path  path=attachments/",
        "+/paths/files-path  path=../app_flutter/attachments/",
    ]


def test_manifest_entries_calls_a_re_encode_a_re_encode(tmp_path):
    """Same document, UTF-8 string pool instead of UTF-16: the CRC moves and
    nothing in the file does."""
    a = apk_with_paths(tmp_path, "a", "attachments/")
    b = apk_with_paths(tmp_path, "b", "attachments/", utf8=True)
    out = run(MANIFEST, "--entries", a, b,
              rows_file(tmp_path, "res/xml/file_paths.xml")).stdout
    assert "~re-encoded; no content change" in out
    assert "+" not in out


def test_manifest_entries_reports_what_is_not_binary_xml(tmp_path):
    a = tmp_path / "a.apk"
    b = tmp_path / "b.apk"
    for p, text in ((a, "one"), (b, "two")):
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("res/xml/x.xml", text)
    out = run(MANIFEST, "--entries", str(a), str(b),
              rows_file(tmp_path, "res/xml/x.xml")).stdout
    assert out.splitlines() == ["=== res/xml/x.xml",
                                "!could not decode as binary XML"]


def test_manifest_entries_reports_an_entry_that_is_gone(tmp_path):
    a = apk_with_paths(tmp_path, "a", "attachments/")
    b = apk_with_paths(tmp_path, "b", "attachments/")
    out = run(MANIFEST, "--entries", a, b,
              rows_file(tmp_path, "res/xml/vanished.xml")).stdout
    assert "!could not decode as binary XML" in out


def test_manifest_entries_skips_a_malformed_row(tmp_path):
    a = apk_with_paths(tmp_path, "a", "attachments/")
    b = apk_with_paths(tmp_path, "b", "x/")
    p = tmp_path / "rows.tsv"
    p.write_text("no tabs here\nres/xml/file_paths.xml\t"
                 "res/xml/file_paths.xml\tres/xml/file_paths.xml\n")
    out = run(MANIFEST, "--entries", a, b, str(p)).stdout
    assert out.count("=== ") == 1


def test_manifest_entries_reports_an_unreadable_archive(tmp_path):
    out = run(MANIFEST, "--entries", "/nope.apk", "/nope.apk",
              rows_file(tmp_path, "x.xml"), expect_ok=False)
    assert out.returncode == 1
    assert "cannot read" in out.stderr


def test_manifest_entries_usage_is_reported():
    out = run(MANIFEST, "--entries", "only-one", expect_ok=False)
    assert out.returncode == 2
    assert "usage" in out.stderr


def test_findings_reports_a_changed_resource_xml(tmp_path):
    out = findings(tmp_path, work={"axml.diff": (
        "=== res/xml/file_paths.xml\n"
        "-/paths/files-path  path=attachments/\n"
        "+/paths/files-path  path=../app_flutter/attachments/\n")})
    assert "[RESOURCE XML CHANGE] 1 binary XML file(s) changed content" in out
    assert "../app_flutter/attachments/" in out


def test_findings_does_not_name_a_decoded_xml_twice(tmp_path):
    """OTHER FILES exists to name entries nothing looked at. Stage 2 looked."""
    out = findings(tmp_path, work={
        "changed.txt": "res/xml/file_paths.xml\nassets/model.bin\n",
        "axml.diff": ("=== res/xml/file_paths.xml\n"
                      "+/paths/files-path  path=x/\n"),
    })
    assert "[RESOURCE XML CHANGE]" in out
    other = out.split("[OTHER FILES]")[1]
    assert "assets/model.bin" in other
    assert "file_paths.xml" not in other


def test_findings_reports_xml_that_only_moved_bytes(tmp_path):
    out = findings(tmp_path, work={
        "changed.txt": "res/xml/backup.xml\n",
        "axml.diff": "=== res/xml/backup.xml\n~re-encoded; no content change\n",
    })
    assert "[RE-ENCODED XML] 1 XML file(s) differ by CRC but not by content" in out
    assert "RESOURCE XML CHANGE" not in out
    assert "OTHER FILES" not in out


def test_findings_reports_xml_it_could_not_decode(tmp_path):
    out = findings(tmp_path, work={
        "axml.diff": "=== res/xml/x.xml\n!could not decode as binary XML\n",
    })
    assert "[UNDECODED XML] 1 changed XML entr(y/ies) could not be decoded" in out
    assert "protobuf" in out
    assert "RESOURCE XML CHANGE" not in out


def test_findings_truncates_a_large_resource_xml_diff(tmp_path):
    rows = "".join("+/paths/p%d  path=%d\n" % (i, i) for i in range(12))
    out = findings(tmp_path, work={"axml.diff": "=== res/xml/big.xml\n" + rows})
    assert "(and 4 more line(s))" in out


def test_findings_ignores_a_stray_line_before_any_xml_header(tmp_path):
    """The artefact is written by stage 2 one entry at a time; a body line with
    no `===` above it means a truncated write, not a finding."""
    out = findings(tmp_path, work={
        "axml.diff": "+/orphan  path=x\n=== res/xml/real.xml\n+/paths  path=y\n",
    })
    assert "res/xml/real.xml" in out
    assert "orphan" not in out


# ---------------------------------------------------------------- resources

import mkarsc                                                  # noqa: E402

ARSC = os.path.join(ROOT, "lib", "arsc.py")


def flatten_table(tmp_path, data, name="resources.arsc"):
    return run(ARSC, write(tmp_path, name, data)).stdout.splitlines()


def values_of(lines):
    """The value lines, as a dict, with the `#` header discarded."""
    return dict(l.split("\t", 1) for l in lines if not l.startswith("#"))


def header_of(lines):
    return dict(l[2:].split(" ", 1) for l in lines if l.startswith("# "))


def test_arsc_reads_every_value_type_it_claims_to(tmp_path):
    v = values_of(flatten_table(tmp_path, mkarsc.sample()))
    assert v["string/app_name"] == "Example"
    assert v["bool/feature_on"] == "true"
    assert v["integer/retry_limit"] == "3"
    assert v["array/flavours[0]"] == "apple"
    assert v["array/flavours[1]"] == "pear"


def test_arsc_escapes_a_value_that_spans_lines(tmp_path):
    """The artefact is compared line by line, so a string resource holding a
    newline must not become two entries."""
    lines = flatten_table(tmp_path, mkarsc.sample())
    assert "string/wrapped\tline one\\nline two" in lines
    assert values_of(lines)["string/wrapped"] == "line one\\nline two"


def test_arsc_leaves_out_the_types_whose_values_are_build_artefacts(tmp_path):
    """A drawable's value is a path the shrinker renames: churn, not change."""
    v = values_of(flatten_table(tmp_path, mkarsc.sample()))
    assert not [k for k in v if k.startswith("drawable/")]


def test_arsc_counts_a_reference_it_declined_to_render(tmp_path):
    """A value the reader skipped has to be visible somewhere, or the count of
    what it did read reads as a complete list."""
    assert header_of(flatten_table(tmp_path, mkarsc.sample()))["references"] == "1"


def test_arsc_compares_only_the_default_configuration(tmp_path):
    """One string per locale would turn one edit into forty identical lines;
    the count of what was left out is what keeps that honest."""
    head = header_of(flatten_table(tmp_path, mkarsc.sample()))
    assert head["nondefault"] == "1"
    assert head["locales"] == "1"
    plain = header_of(flatten_table(tmp_path, mkarsc.sample(locale_entry=False)))
    assert plain["nondefault"] == "0" and plain["locales"] == "0"


def test_arsc_counts_a_configuration_that_is_not_a_language(tmp_path):
    """A density or a night-mode variant is not the default configuration and
    is not a translation either; it is counted and not named."""
    head = header_of(flatten_table(
        tmp_path, mkarsc.sample(locale_entry=False, density_entry=True)))
    assert head["nondefault"] == "1" and head["locales"] == "0"
    assert head["languages"] == ""


def test_arsc_walks_past_a_chunk_it_has_no_use_for(tmp_path):
    """A table carries chunks this reader does not read — a second pool, an
    overlay table — and stopping at one would lose every package after it."""
    data = mkarsc.sample()
    pool_end = 12 + struct.unpack_from("<I", data, 16)[0]
    stray = mkarsc.string_pool(["ignored"])
    # The stray chunk goes between the value pool and the package, so the
    # package is only reached by walking past it.
    padded = data[:pool_end] + stray + data[pool_end:]
    padded = padded[:4] + struct.pack("<I", len(padded)) + padded[8:]
    v = values_of(flatten_table(tmp_path, padded))
    assert v["string/app_name"] == "Example"


def test_arsc_names_the_languages_a_build_carries(tmp_path):
    """A count cannot say which language arrived; the codes can."""
    head = header_of(flatten_table(tmp_path, mkarsc.sample(locale="de")))
    assert head["languages"] == "de" and head["locales"] == "1"


def test_arsc_reads_a_three_letter_locale_packed_into_two_bytes(tmp_path):
    """A code longer than two letters is packed into the same two bytes with
    the high bit set. It is reported in its packed form — this counts locales,
    it does not name them for a reader — but it must not be read as ASCII."""
    packed = bytes([0x80 | 0x41, 0x42])
    head = header_of(flatten_table(tmp_path, mkarsc.sample(locale=packed)))
    assert head["locales"] == "1"
    assert head["languages"] == "#c142"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"flags": mkarsc.FLAG_SPARSE},
        {"flags": mkarsc.FLAG_OFFSET16},
        {"compact": True},
    ],
)
def test_arsc_reads_every_entry_encoding_the_same_way(tmp_path, kwargs):
    """Sparse offsets, 16-bit offsets and compact entries are three encodings
    of one table; a reader that handles one and not another silently reports a
    build's whole string set as changed."""
    plain = values_of(flatten_table(tmp_path, mkarsc.sample()))
    other = values_of(flatten_table(tmp_path, mkarsc.sample(**kwargs)))
    assert other == plain


def test_arsc_names_the_package_when_there_is_more_than_one(tmp_path):
    """Two packages can define the same name with different values, and
    merging them would report one of them as the other's change."""
    one = values_of(flatten_table(tmp_path, mkarsc.sample()))
    assert "string/app_name" in one
    two = values_of(flatten_table(tmp_path, mkarsc.sample(packages=2)))
    assert "com.example:string/app_name" in two
    assert two["com.other1:string/app_name"] == "apple"


def test_arsc_reads_the_table_out_of_an_archive(tmp_path):
    apk = str(tmp_path / "a.apk")
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("resources.arsc", mkarsc.sample())
    assert "string/app_name\tExample" in run(ARSC, apk).stdout.splitlines()


def test_arsc_reads_a_bundles_table_under_its_own_path(tmp_path):
    aab = str(tmp_path / "a.aab")
    with zipfile.ZipFile(aab, "w") as z:
        z.writestr("base/resources.arsc", mkarsc.sample())
    assert "string/app_name\tExample" in run(ARSC, aab).stdout.splitlines()


def test_arsc_reports_an_archive_with_no_table(tmp_path):
    apk = str(tmp_path / "a.apk")
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", "x")
    p = run(ARSC, apk, expect_ok=False)
    assert p.returncode == 1 and "no resources.arsc" in p.stderr


@pytest.mark.parametrize(
    "mangle, message",
    [
        (lambda d: b"XX" + d[2:], "not a resource table"),
        (lambda d: d[:4] + struct.pack("<I", 8) + d[8:], "no value string pool"),
    ],
)
def test_arsc_refuses_bytes_that_are_not_a_table(tmp_path, mangle, message):
    p = run(ARSC, write(tmp_path, "r.arsc", mangle(mkarsc.sample())),
            expect_ok=False)
    assert p.returncode == 1 and message in p.stderr


def test_arsc_rejects_a_chunk_that_runs_past_its_parent(tmp_path):
    """A size field is the only thing keeping the walk inside the file, and a
    truncated table would otherwise read another chunk's bytes as values."""
    data = bytearray(mkarsc.sample())
    data[16:20] = struct.pack("<I", 1 << 30)      # the value pool's own size
    p = run(ARSC, write(tmp_path, "r.arsc", bytes(data)), expect_ok=False)
    assert "runs past the end" in p.stderr


def test_arsc_rejects_a_package_before_the_value_pool(tmp_path):
    """Every string value is an index into that pool; a package reached before
    it would resolve every one of them against nothing."""
    body = mkarsc.package("com.example", mkarsc.TYPES, mkarsc.KEYS,
                          [mkarsc.type_chunk(1, {0: mkarsc.simple_entry(
                              0, mkarsc.TYPE_STRING, 0)})])
    data = struct.pack("<HHII", 0x0002, 12, 12 + len(body), 1) + body
    p = run(ARSC, write(tmp_path, "r.arsc", data), expect_ok=False)
    assert "before the value pool" in p.stderr


def test_arsc_rejects_an_index_past_the_end_of_a_pool(tmp_path):
    """Out of range is a malformed file, not a value to guess at."""
    bad_value = mkarsc.table(
        mkarsc.VALUES,
        [mkarsc.package("com.example", mkarsc.TYPES, mkarsc.KEYS,
                        [mkarsc.type_chunk(1, {0: mkarsc.simple_entry(
                            0, mkarsc.TYPE_STRING, 99)})])])
    p = run(ARSC, write(tmp_path, "a.arsc", bad_value), expect_ok=False)
    assert "past the end of the value pool" in p.stderr
    bad_key = mkarsc.table(
        mkarsc.VALUES,
        [mkarsc.package("com.example", mkarsc.TYPES, mkarsc.KEYS,
                        [mkarsc.type_chunk(1, {0: mkarsc.simple_entry(
                            99, mkarsc.TYPE_STRING, 0)})])])
    p = run(ARSC, write(tmp_path, "b.arsc", bad_key), expect_ok=False)
    assert "key index 99 past the end" in p.stderr


@pytest.mark.parametrize("size", [4096, 2])
def test_arsc_rejects_a_configuration_that_does_not_fit_its_header(tmp_path, size):
    """The declared configuration size decides where the entry offsets begin;
    one that does not fit inside the chunk header would read them out of the
    middle of the configuration itself."""
    data = mkarsc.table(
        mkarsc.VALUES,
        [mkarsc.package("com.example", mkarsc.TYPES, mkarsc.KEYS,
                        [mkarsc.type_chunk(
                            1, {0: mkarsc.simple_entry(0, mkarsc.TYPE_STRING, 0)},
                            config_size=size)])])
    p = run(ARSC, write(tmp_path, "r.arsc", data), expect_ok=False)
    assert "runs past its own chunk header" in p.stderr


def test_arsc_reports_a_table_damaged_past_its_own_checks(tmp_path):
    """A table is a graph of offsets into itself. The named checks cover the
    shapes worth naming; past them the reader still owes the caller a message
    and a status rather than a traceback, because the shell stage discards both
    sides when either read fails."""
    data = mkarsc.sample()
    p = run(ARSC, write(tmp_path, "r.arsc", data[:len(data) - 40]),
            expect_ok=False)
    assert p.returncode == 1
    assert p.stderr.startswith("arsc.py: ") and "Traceback" not in p.stderr


def test_arsc_names_a_type_it_has_no_name_for(tmp_path):
    """A type id past the end of the type pool is a malformed table, but the
    entries under it are still readable, and the reader keeps them rather than
    dropping a package's values over one bad id."""
    data = mkarsc.table(
        mkarsc.VALUES,
        [mkarsc.package("com.example", ["string"], mkarsc.KEYS,
                        [mkarsc.type_chunk(9, {0: mkarsc.simple_entry(
                            0, mkarsc.TYPE_STRING, 0)})])])
    # type9 is not in WANTED, so nothing is emitted — but the read completes.
    assert header_of(flatten_table(tmp_path, data))["values"] == "0"


def test_arsc_diff_reports_what_changed_before_what_was_added(tmp_path):
    """A reworded message is the app's own text before and after, which is the
    most useful line in the report; additions are a list."""
    old = write(tmp_path, "old", "# values 2\nstring/a\tHello\nstring/b\tGone\n")
    new = write(tmp_path, "new", "# values 2\nstring/a\tHi\nstring/c\tNew\n")
    out = run(ARSC, "--diff", old, new).stdout
    assert out.index("--- changed values (1)") < out.index("--- added values (1)")
    assert "      was  Hello" in out and "      now  Hi" in out
    assert "  string/c = New" in out
    assert "--- removed values (1)" in out and "  string/b = Gone" in out


def test_arsc_diff_says_when_nothing_moved(tmp_path):
    """The table's bytes differed — that is why it was read — so silence here
    would be indistinguishable from nobody having looked."""
    same = "# values 1\nstring/a\tHello\n"
    out = run(ARSC, "--diff", write(tmp_path, "o", same),
              write(tmp_path, "n", same)).stdout
    assert "(the resource values are identical)" in out


def test_arsc_usage_is_reported():
    p = run(ARSC, expect_ok=False)
    assert p.returncode == 2 and "usage: arsc.py" in p.stderr


# --------------------------------------------------- findings: new detectors

RES_HEAD = "# values 1\n# nondefault 0\n# locales 0\n# languages \n"


def test_findings_pairs_a_method_that_kept_its_name(tmp_path):
    """A removal and an addition fifty lines apart never mention each other,
    and the one thing the pair says is that a parameter moved."""
    api = "\n".join([
        "com/x/app/Screen\t-\t.method public static final Card(Lcom/x/A;Lcom/x/F;)V",
        "com/x/app/Screen\t+\t.method public static final Card(Lcom/x/A;Lcom/x/G;)V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "[SIGNATURE CHANGE] classes3.dex: 1 method(s)" in out
    assert "Screen.Card(A, F)" in out
    assert "now (A, G)   [+G, -F]" in out
    # And not a second time, as an unrelated addition and an unrelated removal.
    assert "NEW API SURFACE" not in out
    assert "REMOVED API" not in out


def test_findings_names_what_moved_when_the_parameters_did_not(tmp_path):
    """Equal parameter lists mean the flags or the return type moved; printing
    the same list twice reported a change and named none of it."""
    api = "\n".join([
        "com/x/app/K\t-\t.method private static final open(Landroid/content/Context;)V",
        "com/x/app/K\t+\t.method public static final open(Landroid/content/Context;)V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "now private static final void -> public static final void" in out


def test_findings_does_not_guess_which_overload_became_which(tmp_path):
    """Two departures and two arrivals under one name are overloads that
    coexist; pairing them would invent a mapping the file does not state."""
    api = "\n".join([
        "com/x/app/K\t-\t.method public a(I)V",
        "com/x/app/K\t-\t.method public a(J)V",
        "com/x/app/K\t+\t.method public a(Z)V",
        "com/x/app/K\t+\t.method public a(F)V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "SIGNATURE CHANGE" not in out
    assert "[NEW API SURFACE]" in out and "[REMOVED API]" in out


def test_findings_leaves_out_the_compose_stability_marker(tmp_path):
    """$stable is written onto every class the Compose compiler touches, so a
    new class always "gains" one and nobody ever added it."""
    api = "\n".join([
        "com/x/app/K\t+\t.field public static final $stable:I",
        "com/x/app/K\t+\t.field private final real:Z",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "[NEW STATE] classes3.dex: 1 new field(s)" in out
    assert "$stable" not in out


def test_findings_quotes_a_reworded_resource_value(tmp_path):
    """The one place a build keeps both the text a person reads and the name
    its author gave it."""
    out = findings(tmp_path, work={
        "res.old": RES_HEAD + "string/call_failed\tCall failed\n",
        "res.new": RES_HEAD + "string/call_failed\tThe call failed\n",
    })
    assert "[REWORDED RESOURCE] 1 resource value(s) changed" in out
    assert "was  Call failed" in out and "now  The call failed" in out


def test_findings_reports_resources_added_and_removed(tmp_path):
    out = findings(tmp_path, work={
        "res.old": RES_HEAD + "string/gone\tBye\n",
        "res.new": RES_HEAD + "bool/feature_on\ttrue\n",
    })
    assert "[NEW RESOURCE] 1 resource value(s) added" in out
    assert "bool/feature_on = true" in out
    assert "[REMOVED RESOURCE] 1 resource value(s) removed" in out


def test_findings_reports_a_table_that_changed_without_any_value_changing(tmp_path):
    """The bytes differed — that is the only reason the table was read — so
    silence would be indistinguishable from nobody having looked."""
    same = "# values 12\n# nondefault 400\n# locales 3\n# languages de fr it\n"
    out = findings(tmp_path, work={"res.old": same + "string/a\tA\n",
                                   "res.new": same + "string/a\tA\n"})
    assert "[RESOURCES UNCHANGED]" in out
    assert "12 value(s) compared; 400 value(s) in 3 other configuration(s)" in out


def test_findings_names_a_language_the_build_started_carrying(tmp_path):
    out = findings(tmp_path, work={
        "res.old": "# values 1\n# languages de fr\n" + "string/a\tA\n",
        "res.new": "# values 1\n# languages de fr tr\n" + "string/a\tA\n",
    })
    assert "[NEW TRANSLATION] the app now carries values for 1 language(s)" in out
    assert "RESOURCES UNCHANGED" not in out


def test_findings_says_nothing_about_resources_when_only_one_side_was_read(tmp_path):
    """A comparison against a missing side would report every value the build
    has as new."""
    out = findings(tmp_path, work={"res.new": RES_HEAD + "string/a\tA\n"})
    assert "RESOURCE" not in out


# ---------------------------------------------------------------- notes

NOTES = os.path.join(ROOT, "lib", "notes.py")

ABOUT = ("/manifest  package=com.x\n"
         "/manifest  android:versionCode=42\n"
         "/manifest  android:versionName=2.0.0\n")


def notes(tmp_path, work=None, extra_args=()):
    w = tmp_path / "work"
    r = tmp_path / "report"
    w.mkdir(exist_ok=True)
    r.mkdir(exist_ok=True)
    base = {"appns.txt": "com/x/app\n", "manifest.new.flat": ABOUT}
    base.update(work or {})
    for name, text in base.items():
        (w / name).write_text(text)
    run(NOTES, str(w), str(r), *extra_args)
    return (r / "8-notes.md").read_text()


def section(text, name):
    """The lines of one `### name` section of the draft."""
    lines = text.splitlines()
    at = lines.index("### " + name)
    rest = lines[at + 1:]
    end = next((i for i, l in enumerate(rest)
                if l.startswith("### ") or l == END_MARK), len(rest))
    return rest[:end]


END_MARK = "<!-- apkdiff:draft-end -->"

FIX_WORK = {
    "classes3.dex.norm": "CHANGED\tcom/x/app/call/Screen\n"
                         "CHANGED\tcom/x/app/call/Service\n",
    "classes3.dex.api": (
        "com/x/app/call/Service\t+\t.method public final callFailed()V\n"
        "com/x/app/call/Service\t+\t.field private final _failed:Landroid/A;\n"
        "com/x/app/call/Screen\t~\t.method public final render()V\n"),
    "classes3.dex.str.old": "",
    "classes3.dex.str.new": '"Call failed"\n"The link dropped and we gave up."\n',
    "classes3.dex.owner": '"Call failed"\tcom/x/app/call/Screen\n'
                          '"The link dropped and we gave up."\tcom/x/app/call/Screen\n',
    "native.removed.txt": "TODO it didn't work\n",
    "native.added.txt": "Reconnecting didn't work\ncould not tell the service\n",
    "changed.txt": "classes3.dex\nlib/arm64-v8a/librust.so\nlib/x86_64/librust.so\n",
    "names.new": "\n".join("f%d" % i for i in range(500)) + "\n",
}


def test_notes_writes_one_section_between_its_markers(tmp_path):
    text = notes(tmp_path, FIX_WORK)
    assert "<!-- apkdiff:draft-begin -->" in text and END_MARK in text
    body = text[text.index("<!-- apkdiff:draft-begin -->"):text.index(END_MARK)]
    assert body.splitlines()[1] == "## 42"


def test_notes_opens_with_the_scope_the_rest_is_bounded_by(tmp_path):
    """The reader's orientation, and the honest bound on every claim below."""
    text = notes(tmp_path, FIX_WORK)
    scope = text.splitlines()[text.splitlines().index("## 42") + 2]
    assert "3 of 500 archive entries differ" in scope
    assert "2 ABIs of `librust.so`" in scope
    assert "`classes3.dex`" in scope
    assert "Every changed class is in `com.x.app.call`" in scope


def test_notes_tells_one_cross_layer_fix_as_one_entry(tmp_path):
    """The classes, the strings they load and the native evidence naming the
    same thing are one change; stage 7 files them under six headings."""
    rows = section(notes(tmp_path, FIX_WORK), "Fixed")
    assert len([l for l in rows if l.startswith("- **")]) == 1
    fixed = "\n".join(rows)
    assert "stub is now implemented" in fixed
    assert "**Native.** `TODO it didn't work` is no longer in the library" in fixed
    assert "and `Reconnecting didn't work` is new" in fixed
    assert "- **`Screen`.**" in fixed and "- **`Service`.**" in fixed
    assert "1 new method: `callFailed`" in fixed


def test_notes_quotes_the_apps_own_words_as_the_notes_file_does(tmp_path):
    text = notes(tmp_path, FIX_WORK)
    assert "**“The link dropped and we gave up.”**" in text
    # Never inside the lead sentence, which is bold already: markdown renders
    # the nested pair as literal asterisks.
    for line in text.splitlines():
        if line.startswith("- **"):
            assert "**“" not in line


def test_notes_files_a_new_type_threaded_through_signatures_as_new(tmp_path):
    api = "\n".join([
        "com/x/app/ui/Gestures\t+\t.method public <init>()V",
        "com/x/app/ui/Screen\t-\t.method public Card(Lcom/x/app/ui/A;Lkotlin/F;)V",
        "com/x/app/ui/Screen\t+\t.method public Card(Lcom/x/app/ui/A;Lcom/x/app/ui/Gestures;)V",
    ]) + "\n"
    text = notes(tmp_path, {
        "classes3.dex.norm": "NEW\tcom/x/app/ui/Gestures\n"
                             "CHANGED\tcom/x/app/ui/Screen\n",
        "classes3.dex.api": api,
    })
    new = "\n".join(section(text, "New"))
    assert "`Gestures` is a new type, and it now appears in 1 changed method" in new
    assert "`Card(A, F)` now takes `(A, Gestures)` — +`Gestures`, -`F`" in new


def test_notes_keeps_a_package_of_unrelated_changes_apart(tmp_path):
    """A package with a handful of changed classes is one change; a screen with
    fifteen is several, and only the evidence says which go together."""
    rows, norm = [], []
    for i in range(6):
        norm.append("CHANGED\tcom/x/app/ui/C%d" % i)
        rows.append("com/x/app/ui/C%d\t+\t.method public m%d()V" % (i, i))
    text = notes(tmp_path, {"classes3.dex.norm": "\n".join(norm) + "\n",
                            "classes3.dex.api": "\n".join(rows) + "\n"})
    assert len([l for l in section(text, "Changed") if l.startswith("- **")]) == 6


def test_notes_joins_classes_that_share_the_words_they_print(tmp_path):
    """Two shared content words is the threshold every pairing here uses."""
    norm = "".join("CHANGED\tcom/x/app/ui/C%d\n" % i for i in range(6))
    api = "".join("com/x/app/ui/C%d\t~\t.method public m()V\n" % i
                  for i in range(6))
    text = notes(tmp_path, {
        "classes3.dex.norm": norm,
        "classes3.dex.api": api,
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"upload queue drained"\n"upload queue stalled"\n',
        "classes3.dex.owner": '"upload queue drained"\tcom/x/app/ui/C0\n'
                              '"upload queue stalled"\tcom/x/app/ui/C1\n',
    })
    entries = [l for l in section(text, "Changed") if l.startswith("- **")]
    assert len(entries) == 5           # C0 and C1 joined, the rest stand alone


def test_notes_reports_a_native_only_change_as_its_own_entry(tmp_path):
    """The library is stripped, so there is no class to name it with — and an
    unattached native change is still a change."""
    text = notes(tmp_path, {"native.added.txt": "Failed to send the packet\n"})
    assert "in the native library now reports a condition" in text


def test_notes_ends_with_a_ledger_accounting_for_every_finding(tmp_path):
    """The one mechanical guarantee: cutting a line stays a decision, and
    losing one does not happen."""
    text = notes(tmp_path, FIX_WORK)
    ledger = text[text.index("evidence ledger"):]
    stated = int(re.search(r"(\d+) findings?, all accounted", ledger).group(1))
    assert stated == len([l for l in ledger.splitlines()
                          if l.startswith("  ") and "  " in l.strip()])
    for kind in ("STUB IMPLEMENTED", "CHANGE AREA", "NEW USER-FACING TEXT"):
        assert kind in ledger


def test_notes_puts_what_the_run_could_not_see_in_the_notes_section(tmp_path):
    """A caveat is a fact about the comparison, never a change the build made."""
    text = notes(tmp_path, {"no-body-diff.txt": "apktool\n"})
    assert "method bodies were not compared" in "\n".join(section(text, "Notes"))
    assert "### Changed" not in text


def test_notes_says_what_it_suppressed_rather_than_leaving_it_unsaid(tmp_path):
    assert "R8 re-sharding" in "\n".join(section(notes(tmp_path, FIX_WORK),
                                                 "Notes"))


def test_notes_inserts_a_section_into_a_notes_file(tmp_path):
    target = tmp_path / "release-notes.md"
    target.write_text("# `com.x` — release notes\n\nBuilds 1 -> 42.\n\n---\n\n"
                      "## 41\n\nolder.\n")
    notes(tmp_path, FIX_WORK, extra_args=("--into", str(target)))
    body = target.read_text()
    assert body.index("## 42") < body.index("## 41")
    assert body.startswith("# `com.x` — release notes")
    assert "apkdiff:draft-begin" not in body      # the markers are scaffolding
    assert "evidence ledger" not in body


def test_notes_refuses_to_overwrite_a_section_somebody_wrote(tmp_path):
    """Replacing it silently would discard writing this tool cannot redo."""
    target = tmp_path / "n.md"
    target.write_text("# n\n\n---\n\n## 42\n\nhand written.\n")
    w = tmp_path / "work"
    w.mkdir()
    (w / "manifest.new.flat").write_text(ABOUT)
    p = run(NOTES, str(w), str(tmp_path), "--into", str(target),
            expect_ok=False)
    assert p.returncode == 1 and "already has a section for 42" in p.stderr
    assert target.read_text().endswith("hand written.\n")


def test_notes_refuses_a_file_with_nowhere_to_insert(tmp_path):
    target = tmp_path / "n.md"
    target.write_text("# n\n\nno separator here.\n")
    w = tmp_path / "work"
    w.mkdir()
    (w / "manifest.new.flat").write_text(ABOUT)
    p = run(NOTES, str(w), str(tmp_path), "--into", str(target),
            expect_ok=False)
    assert 'no "---" separator' in p.stderr


def test_notes_usage_is_reported(tmp_path):
    p = run(NOTES, str(tmp_path), expect_ok=False)
    assert p.returncode == 2 and "usage: notes.py" in p.stderr


def test_notes_states_what_a_capped_list_left_out(tmp_path):
    """A list that silently stops is a list a reader trusts to be complete."""
    api = "".join("com/x/app/K\t+\t.method public m%02d()V\n" % i
                  for i in range(9))
    api += "".join("com/x/app/K\t-\t.method public g%02d(Lcom/x/A;)V\n"
                   "com/x/app/K\t+\t.method public g%02d(Lcom/x/B;)V\n" % (i, i)
                   for i in range(6))
    text = notes(tmp_path, {"classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
                            "classes3.dex.api": api})
    assert "(and 3 more)" in text
    assert "(and 2 more signature changes)" in text


def test_notes_names_a_class_that_went_away(tmp_path):
    text = notes(tmp_path, {
        "classes3.dex.norm": "GONE\tcom/x/app/Old\nCHANGED\tcom/x/app/K\n",
        "classes3.dex.api": "com/x/app/K\t-\t.field private f:Z\n",
    })
    assert "`Old` is gone" in text
    assert "no longer has 1 field" in text


def test_notes_reports_a_method_that_only_changed_what_it_returns(tmp_path):
    api = ("com/x/app/K\t-\t.method public load()Ljava/lang/String;\n"
           "com/x/app/K\t+\t.method public load()Lcom/x/Result;\n")
    text = notes(tmp_path, {"classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
                            "classes3.dex.api": api})
    assert "`load()` now returns `Result` rather than `String`" in text


def test_notes_reports_a_method_only_its_callers_can_now_reach(tmp_path):
    api = ("com/x/app/K\t-\t.method private open(Landroid/A;)V\n"
           "com/x/app/K\t+\t.method public open(Landroid/A;)V\n")
    text = notes(tmp_path, {"classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
                            "classes3.dex.api": api})
    assert "is declared `public`, and was `private`" in text


def test_notes_names_several_new_types_when_none_was_threaded(tmp_path):
    text = notes(tmp_path, {
        "classes3.dex.norm": "NEW\tcom/x/app/A\nNEW\tcom/x/app/B\n",
    })
    assert "`A`, `B` are new." in text


def test_notes_leads_with_the_class_that_gained_something(tmp_path):
    """A topic where one class was rewritten and another gained a method led
    with the wrong subject over a bullet showing the method elsewhere."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/Big\nCHANGED\tcom/x/app/Small\n",
        "classes3.dex.api": (
            "com/x/app/Big\t~\t.method public a()V\n"
            "com/x/app/Big\t~\t.method public b()V\n"
            "com/x/app/Small\t+\t.method public purge()V\n"),
    })
    assert "`Small` gained 1 method." in text


def test_notes_reports_a_manifest_change_as_an_entry_of_its_own(tmp_path):
    """An app that asks for a new permission has changed for the person using
    it whether or not a class moved."""
    w = tmp_path / "work"
    w.mkdir()
    r = tmp_path / "report"
    r.mkdir()
    (w / "manifest.new.flat").write_text(ABOUT)
    (r / "2-manifest.txt").write_text(
        "+/manifest/uses-permission  android:name=android.permission.CAMERA\n")
    run(NOTES, str(w), str(r))
    text = (r / "8-notes.md").read_text()
    assert "The app requests 1 permission(s)." in "\n".join(section(text, "New"))


def test_notes_lists_the_entries_no_stage_read_under_notes(tmp_path):
    text = notes(tmp_path, {"added.txt": "assets/model.bin\n"})
    assert "non-code file(s) added" in "\n".join(section(text, "Notes"))


def test_notes_caps_the_archive_entries_it_names(tmp_path):
    work = dict(FIX_WORK)
    work["changed.txt"] = "".join("assets/a%d.bin\n" % i for i in range(12))
    text = notes(tmp_path, work)
    assert "and 4 other entries" in text


def test_notes_leaves_the_bundled_libraries_out_of_the_entries(tmp_path):
    """A build carries far more third-party code than app code, and AndroidX
    churns on its own schedule."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "NEW\tandroidx/compose/Runtime\n"
                             "CHANGED\tcom/x/app/K\n",
        "classes3.dex.api": (
            "androidx/compose/Runtime\t+\t.method public go()V\n"
            "androidx/compose/Runtime\t-\t.method public go(I)V\n"
            "com/x/app/K\t+\t.method public real()V\n"),
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"androidx message"\n',
        "classes3.dex.owner": '"androidx message"\tandroidx/compose/Runtime\n',
    })
    assert "androidx" not in text[:text.index("evidence ledger")]
    assert "`K` gained 1 method." in text


def test_notes_places_a_native_string_by_the_literal_it_names(tmp_path):
    """A literal on the other side of the JNI boundary naming the same thing
    is the strongest link there is — stronger than sharing a package."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/one/A\nCHANGED\tcom/x/app/two/B\n",
        "classes3.dex.api": ("com/x/app/one/A\t~\t.method public a()V\n"
                             "com/x/app/two/B\t~\t.method public b()V\n"),
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"prekey handshake rejected"\n"unrelated text"\n',
        "classes3.dex.owner": '"prekey handshake rejected"\tcom/x/app/two/B\n'
                              '"unrelated text"\tcom/x/app/one/A\n',
        "native.added.txt": "failed to handle the prekey handshake\n",
    })
    entries = [l for l in text.splitlines() if l.startswith("    - **Native.**")]
    assert len(entries) == 1
    at = text.index("**Native.**")
    assert "`B`" in text[text.rindex("- **", 0, at):at + 400]


def test_notes_places_a_native_string_by_shared_vocabulary(tmp_path):
    """Failing a literal, a topic that talks about the same things is the next
    best evidence — and two shared words is the threshold, not one."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/upload/A\n"
                             "CHANGED\tcom/x/app/other/B\n",
        "classes3.dex.api": ("com/x/app/upload/A\t~\t.method public a()V\n"
                             "com/x/app/other/B\t~\t.method public b()V\n"),
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"upload queue drained"\n',
        "classes3.dex.owner": '"upload queue drained"\tcom/x/app/upload/A\n',
        "native.added.txt": "upload queue is full\n",
    })
    at = text.index("**Native.**")
    assert "`A`" in text[text.rindex("- **", 0, at):at + 300]


def test_notes_keeps_a_removed_native_string_that_is_not_a_stub(tmp_path):
    """Nothing implemented it, so it is not a stub — but the condition it
    reported is either gone or no longer reported, and the ledger below claims
    this finding was absorbed into an entry."""
    text = notes(tmp_path, {
        "native.removed.txt": "could not open the session file\n",
        "native.added.txt": "Failed to send the packet\n"})
    body = text[:text.index("evidence ledger")]
    assert "the library no longer contains 1 log string" in body
    assert "`could not open the session file`" in body
    assert "REMOVED ERROR HANDLING absorbed" in re.sub(r" +", " ", text)


@pytest.mark.parametrize("work,evidence", (
    ({"native.added.txt": "Failed to send the packet\n"},
     "Failed to send the packet"),
    ({"native.removed.txt": "could not open the session file\n"},
     "could not open the session file"),
    ({"native.removed.txt": "TODO not implemented yet\n",
      "native.added.txt": "could not open the session file\n"},
     "TODO not implemented yet"),
))
def test_notes_keeps_native_evidence_no_class_can_hold(tmp_path, work, evidence):
    """A library change the dex gives nowhere to belong gets a topic of its
    own — one for each kind of evidence that topic can carry, alone.

    The case above passes a removal and an addition together, so the topic was
    retained by the addition and the removal rode along; a build whose library
    only lost strings dropped the topic and every string on it, while the
    ledger still called REMOVED ERROR HANDLING absorbed and the scope line
    called the build packaging-only."""
    text = notes(tmp_path, work)
    body = text[:text.index("evidence ledger")]
    assert "`%s`" % evidence in body, body
    assert "packaging-only" not in body


def test_notes_leads_with_the_text_the_app_now_shows(tmp_path):
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
        "classes3.dex.api": "com/x/app/K\t+\t.method public show()V\n",
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"Your message was not delivered"\n',
        "classes3.dex.owner": '"Your message was not delivered"\tcom/x/app/K\n',
    })
    assert ("The app shows 1 message it did not have before — “Your message "
            "was not delivered”, loaded by `K`.") in text


def test_notes_says_when_the_library_moved_and_the_dex_did_not(tmp_path):
    """Neither a stub nor a failure path: a library that gained log strings
    this file cannot classify is reported as exactly that."""
    text = notes(tmp_path, {"native.added.txt": "starting the media session\n"})
    assert "The native library gained 1 log string and nothing in the dex" in text


def test_notes_does_not_join_two_classes_over_one_shared_word(tmp_path):
    norm = "".join("CHANGED\tcom/x/app/ui/C%d\n" % i for i in range(6))
    api = "".join("com/x/app/ui/C%d\t~\t.method public m()V\n" % i
                  for i in range(6))
    text = notes(tmp_path, {
        "classes3.dex.norm": norm,
        "classes3.dex.api": api,
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": ('"upload queue drained"\n'
                                 '"upload queue stalled"\n'
                                 '"upload finished"\n'),
        "classes3.dex.owner": ('"upload queue drained"\tcom/x/app/ui/C0\n'
                               '"upload queue stalled"\tcom/x/app/ui/C1\n'
                               '"upload finished"\tcom/x/app/ui/C2\n'),
    })
    entries = [l for l in section(text, "Changed") if l.startswith("- **")]
    assert len(entries) == 5           # C2 shares only "upload", so it stands alone


def test_findings_ignores_a_member_row_with_no_name_in_it(tmp_path):
    """The artefacts are written by another process; a row that is not a
    declaration is a truncated write, not a method."""
    api = ("com/x/app/K\t+\t.method public but-no-parentheses\n"
           "com/x/app/K\t+\t.method public real()V\n")
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "SIGNATURE CHANGE" not in out
    assert "[NEW API SURFACE] classes3.dex: 2 new method(s)" in out


RICH_WORK = dict(FIX_WORK, **{
    "classes3.dex.norm": "CHANGED\tcom/x/app/call/Screen\n"
                         "CHANGED\tcom/x/app/call/Service\n"
                         "NEW\tcom/x/app/call/Ticket\n"
                         "GONE\tcom/x/app/call/Legacy\n",
    "classes3.dex.api": (
        "com/x/app/call/Service\t+\t.method public final callFailed()V\n"
        "com/x/app/call/Service\t+\t.field private final _failed:Landroid/A;\n"
        "com/x/app/call/Service\t-\t.field private final _old:Landroid/A;\n"
        "com/x/app/call/Service\t-\t.method public final retire()V\n"
        "com/x/app/call/Screen\t~\t.method public final render()V\n"
        "com/x/app/call/Screen\t-\t.method public show(Lcom/x/A;)V\n"
        "com/x/app/call/Screen\t+\t.method public show(Lcom/x/app/call/Ticket;)V\n"),
})

WORD_IN_EVIDENCE = re.compile(r"[A-Za-z_$][\w$]{3,}")
# Words a draft carries whatever it found. Matching on one of these would make
# the check below pass on a body that says nothing about the finding at all —
# "1 method was rewritten" satisfies any finding whose evidence says `method`.
LEDGER_BOILERPLATE = frozenset("""
    method methods field fields class classes string strings public private
    final static native archive entries build library failure paths now new
    added removed rewritten changed replaced text message messages user facing
    state surface signature behaviour error handling stub implemented every
    from with that this into loads name names top-level side other
""".split())
# Two kinds are absorbed into the opening paragraph rather than into an entry:
# the build's identity is its heading, and the package a change is confined to
# is the scope sentence. Everything else has to be inside an entry, which is
# what the ledger's own wording claims of all of them.
SCOPE_KINDS = frozenset(("VERSION", "CHANGE AREA"))


def findings_by_kind(report):
    """Stage 7's report as kind -> list of findings, each a list of lines."""
    out, kind = {}, None
    for line in report.splitlines():
        if line.startswith("["):
            kind = line[1:line.index("]")]
            out.setdefault(kind, []).append([line[line.index("]") + 1:]])
        elif line.startswith("  ") and kind:
            out[kind][-1].append(line)
    return out


@pytest.mark.parametrize("work,least", (
    (RICH_WORK, 6),
    (FIX_WORK, 3),
    ({"native.removed.txt": "could not open the session file\n"}, 1),
))
def test_notes_ledger_does_not_claim_an_absorption_it_did_not_make(
        tmp_path, work, least):
    """The ledger's promise is that a finding cannot fall out of the draft
    unnoticed. That is only true while every kind it calls absorbed really is
    somewhere in the body, so this checks the promise rather than the list: a
    word out of the finding's own evidence, one the draft does not print
    anyway, has to appear where the ledger says the finding went.

    Over several builds rather than one, because the shape of a build decides
    which entries exist at all — a library that only lost strings had its
    findings put on a topic the draft then discarded, and a check that only
    ever ran against a build rich in dex changes could not see it."""
    text = notes(tmp_path, work)
    draft = text[:text.index("evidence ledger")]
    entries = draft[draft.index("### "):]
    scope = draft[:draft.index("### ")]
    scope_words = set(WORD_IN_EVIDENCE.findall(scope))
    rows = [re.match(r"  (\S.*?)\s\s+(\S+)\s\s+(.*)", l)
            for l in text[text.index("evidence ledger"):].splitlines()]
    absorbed = [m for m in rows if m and m.group(2) == "absorbed"]
    assert len(absorbed) >= least
    report = run(FINDINGS, str(tmp_path / "work"), str(tmp_path / "report")).stdout
    by_kind = findings_by_kind(report)
    kinds = set(by_kind)
    for m in absorbed:
        kind = m.group(1)
        assert kind in kinds
        # Every finding of that kind, not just one: the ledger names a kind
        # once, so one finding of it going missing is the same broken promise.
        for finding in by_kind[kind]:
            words = {w for line in finding
                     for w in WORD_IN_EVIDENCE.findall(line)}
            words -= LEDGER_BOILERPLATE
            where = scope if kind in SCOPE_KINDS else entries
            if kind not in SCOPE_KINDS:
                words -= scope_words
            assert any(w in where for w in words), (kind, sorted(words))
    # And every finding stage 7 made is named in the ledger, whatever became
    # of it.
    for kind in kinds:
        assert kind in text[text.index("evidence ledger"):]


def test_notes_carries_the_evidence_of_what_it_absorbed(tmp_path):
    """Absorbing a finding means its content is somewhere in the entries, not
    that its heading was dropped."""
    text = notes(tmp_path, RICH_WORK)
    body = text[:text.index("evidence ledger")]
    for token in ("callFailed", "_failed", "retire", "_old", "render",
                  "Ticket", "Legacy", "show", "Call failed",
                  "Reconnecting didn't work", "com.x.app.call"):
        assert token in body, token


def test_notes_leaves_out_the_literals_the_compiler_wrote(tmp_path):
    """Kotlin @Metadata blobs and Compose source-line markers churn on every
    build without meaning anything."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
        "classes3.dex.api": "com/x/app/K\t~\t.method public m()V\n",
        "classes3.dex.str.old": "",
        "classes3.dex.str.new": '"Screen.kt:412"\n"a real message"\n',
        "classes3.dex.owner": '"Screen.kt:412"\tcom/x/app/K\n'
                              '"a real message"\tcom/x/app/K\n',
    })
    body = text[:text.index("evidence ledger")]
    assert "a real message" in body and "Screen.kt:412" not in body


def test_notes_reports_a_swarm_of_lambdas_under_the_class_it_belongs_to(tmp_path):
    """Compose emits a swarm of anonymous classes per edited function; the
    top-level class they belong to is the change."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "NEW\tcom/x/app/K$1\n"
                             "NEW\tcom/x/app/ComposableSingletons$KKt\n"
                             "CHANGED\tcom/x/app/K\n",
        "classes3.dex.api": "com/x/app/K\t+\t.method public m()V\n",
    })
    body = text[:text.index("evidence ledger")]
    assert "K$1" not in body and "ComposableSingletons" not in body
    assert "`K` gained 1 method." in body


def test_notes_falls_back_to_the_namespace_for_a_class_it_cannot_place(tmp_path):
    """The member artefacts name classes fully, but a build whose norm listing
    does not mention one still has to file it somewhere."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "",
        "classes3.dex.api": "com/x/app/Orphan\t+\t.method public m()V\n",
    })
    assert "`Orphan` gained 1 method." in text


def test_notes_places_a_native_string_by_the_words_in_a_class_name(tmp_path):
    """A class that loads no literals still says what it is about in its name,
    which is the last evidence available for where a native string belongs."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/one/UploadQueueService\n"
                             "CHANGED\tcom/x/app/two/Unrelated\n",
        "classes3.dex.api": (
            "com/x/app/one/UploadQueueService\t~\t.method public a()V\n"
            "com/x/app/two/Unrelated\t~\t.method public b()V\n"),
        "native.added.txt": "the upload queue is full\n",
    })
    at = text.index("**Native.**")
    assert "UploadQueueService" in text[text.rindex("- **", 0, at):at]



def test_native_keeps_the_word_glued_to_an_identifier_run(tmp_path):
    """The message's last word is glued to the identifier packed after it, and
    dropping the whole token dropped the word with it: the report said "Call
    failed; telling the" of a literal reading "...telling the service"."""
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so",
                b"\x00Call failed; telling the serviceRelativeUrlWithBase\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "Call failed; telling the service"]


def test_native_keeps_a_capitalised_word_whole(tmp_path):
    """A token STARTING with a capital is an ordinary CamelCase word of the
    message, not two literals meeting — cutting at its internal transition
    would report "init Media" of a message about a MediaCodec."""
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so", b"\x00failed to init MediaCodecForAudio\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "failed to init MediaCodecForAudio"]


def test_native_cuts_a_tail_the_other_build_already_has(tmp_path):
    """Two literals packed with no case change between them look like one
    sentence, and no rule about shape can tell them apart. That the second half
    is not new can."""
    old = write(tmp_path, "old.so", b"\x00failed to spawn the worker thread\x00")
    new = write(tmp_path, "new.so",
                b"\x00cannot report the failed callfailed to spawn the worker "
                b"thread\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "cannot report the failed call"]


def test_native_keeps_a_sentence_that_merely_ends_alike(tmp_path):
    """The join has to fall mid-token, because that is what a packed boundary
    is. A shared tail that begins at a space is a sentence carrying on, and
    cutting it truncated a real message at "could not tell the service the"."""
    old = write(tmp_path, "old.so", b"\x00the previous call failed\x00")
    new = write(tmp_path, "new.so",
                b"\x00could not tell the service the call failed\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "could not tell the service the call failed"]


def test_native_drops_a_candidate_that_is_only_a_boundary(tmp_path):
    """Everything but a fragment too small to have been a candidate in its own
    right is text the other build already has: what is new here is where two
    old literals meet, and a boundary is not a message."""
    old = write(tmp_path, "old.so", b"\x00the queue was drained here\x00")
    new = write(tmp_path, "new.so",
                b"\x00xy queuethe queue was drained here\x00"
                b"a genuinely new message here\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "a genuinely new message here"]


def test_native_recovers_a_head_the_scan_cut_off(tmp_path):
    """A literal packed inside a long printable run arrives with its first word
    missing, because the scan caps a candidate at 89 characters. The bytes are
    in front of it, and the walk back stops where the previous literal ended —
    at a capital it cannot be part of."""
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so",
                b"\x00CallFailed()Vno room left in the queue for this\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "no room left in the queue for this"]


def test_native_will_not_borrow_a_word_from_a_run_with_no_boundary(tmp_path):
    """A walk that runs out of budget inside unbroken lowercase text never
    found where the literal starts. Prepending what it crossed would glue the
    previous message's tail onto this one, which is inventing a sentence."""
    # 86 characters of unbroken lowercase, so the scan's 89-character cap
    # swallows the message's first word and leaves no capital to walk back to.
    lead = b"the quick brown fox jumps over the lazy dog and keeps on running for a while and then "
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so",
                b"\x00" + lead + b"no room left in the queue for this\x00")
    got = reported(run(NATIVE, old, new).stdout)
    # Truncated, and honest about it, rather than completed from the text in
    # front of it.
    assert "room left in the queue for this" in got
    assert not any("then" in g and "room left" in g for g in got)


def test_native_does_not_reglue_the_fragment_it_dropped(tmp_path):
    """The left extension walks back over lowercase letters, which is exactly
    what the dropped first token is made of. Stopping on a capital that a
    lowercase letter runs into keeps "adGot" out."""
    old = write(tmp_path, "old.so", b"\x00\x00")
    new = write(tmp_path, "new.so", b"\x00adGot empty sample here\x00")
    assert reported(run(NATIVE, old, new).stdout) == ["empty sample here"]


def test_native_does_not_extend_a_head_the_other_build_already_has(tmp_path):
    """A cut head the other build carries is an old literal: there is nothing
    to recover in front of it, and the re-test drops it. Tested because the
    same fact is why the walk needs to check that only once — a longer string
    cannot be present where a shorter one is not."""
    old = write(tmp_path, "old.so",
                b"\x00no room left in the queue\x00the queue was drained here\x00")
    new = write(tmp_path, "new.so",
                b"\x00no room left in the queuethe queue was drained here\x00"
                b"zz another new message\x00")
    assert reported(run(NATIVE, old, new).stdout) == ["zz another new message"]


def test_native_drops_a_boundary_with_nothing_in_front_of_it(tmp_path):
    """A head too small to have been a candidate in its own right leaves
    nothing but the join, and a join is not a message."""
    old = write(tmp_path, "old.so",
                b"\x00set_profile returned no profile at all\x00")
    new = write(tmp_path, "new.so",
                b"\x00xyset_profile returned no profile at all\x00"
                b"a genuinely new message here\x00")
    assert reported(run(NATIVE, old, new).stdout) == [
        "a genuinely new message here"]



def test_findings_reports_a_method_the_library_started_naming(tmp_path):
    """GetMethodID puts the descriptor in the library's .rodata as a C string,
    so a Java method the native side intends to call is a fact about the bytes
    rather than a guess from wording. The JNI rule cannot see it: it looks for
    a descriptor-shaped literal in the dex, and a method only the native side
    names has none."""
    w = tmp_path / "work"
    (w / "new" / "lib" / "arm64").mkdir(parents=True)
    (w / "old" / "lib" / "arm64").mkdir(parents=True)
    (w / "new" / "lib" / "arm64" / "a.so").write_bytes(
        b"\x00callFailed()V\x00tearDown()V\x00")
    (w / "old" / "lib" / "arm64" / "a.so").write_bytes(b"\x00tearDown()V\x00")
    out = findings(tmp_path, work={
        "appns.txt": "com/x/app\n",
        "classes3.dex.api": (
            "com/x/app/Service\t+\t.method public final callFailed()V\n"
            "com/x/app/Service\t+\t.method public final tearDown()V\n"
            "com/x/app/Service\t+\t.method public final unnamed()V\n"),
    })
    assert "[NEW NATIVE UPCALL] classes3.dex: the library now names 1" in out
    assert "Service.callFailed()V" in out
    # tearDown was already named, and unnamed is named by neither.
    assert "tearDown()V" not in out.split("[NEW NATIVE UPCALL]")[1].split("\n\n")[0]


def test_notes_files_an_upcall_under_the_class_that_holds_it(tmp_path):
    """As a finding of its own it reads as an unrelated fact about the dex
    rather than as the missing half of the fix above it — and a nested class's
    upcall belongs to the class that contains it, which is the unit a topic is
    keyed by."""
    w = tmp_path / "work"
    (w / "new" / "lib" / "arm64").mkdir(parents=True)
    (w / "old" / "lib" / "arm64").mkdir(parents=True)
    (w / "new" / "lib" / "arm64" / "a.so").write_bytes(b"\x00onDone()V\x00")
    (w / "old" / "lib" / "arm64" / "a.so").write_bytes(b"\x00\x00")
    text = notes(tmp_path, {
        "classes3.dex.api": "com/x/app/K$Handle\t+\t.method public onDone()V\n",
        "classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
    })
    assert "**Native.** the library now calls `K.onDone()V`" in text
    assert "NEW NATIVE UPCALL" not in text[:text.index("evidence ledger")]



def test_findings_leaves_out_the_accessors_a_new_field_implies(tmp_path):
    """Kotlin writes a getter and a setter for every property; a class that
    gained a field otherwise "gains" two more members nobody wrote, saying
    nothing the field has not said."""
    api = "\n".join([
        "com/x/app/S\t+\t.field private final _callFailed:Landroid/A;",
        "com/x/app/S\t+\t.method public final getCallFailed()Landroid/A;",
        "com/x/app/S\t+\t.method public final callFailed()V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "[NEW API SURFACE] classes3.dex: 1 new method(s)" in out
    assert "getCallFailed" not in out
    assert "S.callFailed()" in out


def test_findings_keeps_an_accessor_that_stands_alone(tmp_path):
    """A getter arriving without its field is a property becoming readable
    from outside, which is a real change to the class's surface."""
    api = "com/x/app/S\t+\t.method public final getName()Ljava/lang/String;\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "getName" in out


def test_findings_leaves_out_what_a_changed_field_regenerates(tmp_path):
    """The constructor of a class whose fields changed changed with them, and
    so did everything Kotlin writes for a data class."""
    api = "\n".join([
        "com/x/app/T\t+\t.field private final stackParts:Ljava/util/List;",
        "com/x/app/T\t~\t.method public constructor <init>()V",
        "com/x/app/T\t~\t.method public equals(Ljava/lang/Object;)Z",
        "com/x/app/T\t~\t.method public hashCode()I",
        "com/x/app/T\t~\t.method public final render()V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "1 method(s) rewritten" in out
    assert "render()" in out
    for gone in ("<init>", "equals", "hashCode"):
        assert gone not in out


def test_findings_keeps_a_constructor_rewritten_on_its_own(tmp_path):
    """A constructor rewritten on a class whose fields all held is a real
    change to what it does on construction."""
    api = "com/x/app/T\t~\t.method public constructor <init>()V\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "<init>" in out


def test_findings_leaves_out_the_members_marked_synthetic(tmp_path):
    """ACC_SYNTHETIC means, by definition, not present in the source: the
    bridge for a default argument, the DefaultConstructorMarker overload, the
    accessor an inner class reaches its outer through."""
    api = "\n".join([
        "com/x/app/T\t+\t.method public synthetic constructor <init>(IL"
        "kotlin/jvm/internal/DefaultConstructorMarker;)V",
        "com/x/app/T\t~\t.method public static synthetic bridge$x()V",
        "com/x/app/T\t+\t.method public final real()V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "T.real()" in out
    assert "synthetic" not in out
    assert "DefaultConstructorMarker" not in out


def test_findings_leaves_out_a_mangled_data_class_member(tmp_path):
    """Kotlin mangles the name of any member whose signature mentions an inline
    value class, and the hash changes whenever a parameter's type does."""
    api = "\n".join([
        "com/x/app/T\t+\t.method public final copy-ECKN2tM(I)Lcom/x/app/T;",
        "com/x/app/T\t+\t.method public equals-impl0(Ljava/lang/Object;)Z",
        "com/x/app/T\t+\t.method public final real()V",
    ]) + "\n"
    out = findings(tmp_path, work={"appns.txt": "com/x/app\n",
                                   "classes3.dex.api": api})
    assert "[NEW API SURFACE] classes3.dex: 1 new method(s)" in out
    assert "copy-" not in out and "equals-impl" not in out



def test_notes_shows_a_reworded_message_as_a_pair(tmp_path):
    """The pair says more than either half, and the entry already lists the new
    string among what its class loads — so left as a finding of its own this
    reports one edit twice and calls one of them an addition."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
        "classes3.dex.api": "com/x/app/K\t~\t.method public m()V\n",
        "classes3.dex.str.old": '"Failed to get the live view"\n',
        "classes3.dex.str.new": '"Failed to get the live view for guid="\n',
        "classes3.dex.owner":
            '"Failed to get the live view for guid="\tcom/x/app/K\n',
    })
    body = text[:text.index("evidence ledger")]
    assert ('reworded **“Failed to get the live view”** to '
            '**“Failed to get the live view for guid=”**') in body
    assert "loads" not in body
    assert "REWORDED TEXT            absorbed" in re.sub(r" +(?=absorbed)",
                                                         "            ", text)


def test_notes_keeps_a_rewording_it_cannot_place(tmp_path):
    """No app class is recorded as loading the new text, so no entry can carry
    it, and the ledger has to say the finding stands on its own."""
    text = notes(tmp_path, {
        "classes3.dex.norm": "CHANGED\tcom/x/app/K\n",
        "classes3.dex.api": "com/x/app/K\t~\t.method public m()V\n",
        "classes3.dex.str.old": '"Failed to get the live view"\n',
        "classes3.dex.str.new": '"Failed to get the live view for guid="\n',
    })
    assert "message(s) shown to the user were reworded" in text
    assert "REWORDED TEXT            Changed" in re.sub(r" +(?=Changed)",
                                                        "            ", text)


def test_manifest_xml_with_a_dtd_is_refused(tmp_path):
    """An entity-expansion bomb is refused before it is expanded."""
    bomb = ('<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><manifest>&b;</manifest>')
    path = tmp_path / "AndroidManifest.xml"
    path.write_text(bomb)
    ns = runpy.run_path(MANIFEST, run_name="not_main")
    with pytest.raises(ns["axml"].Bad):
        ns["root_of"](str(path))


def test_plain_manifest_xml_still_parses(tmp_path):
    path = tmp_path / "AndroidManifest.xml"
    path.write_text('<manifest package="com.example"/>')
    ns = runpy.run_path(MANIFEST, run_name="not_main")
    assert ns["root_of"](str(path)).get("package") == "com.example"
