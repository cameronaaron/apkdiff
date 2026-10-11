"""Flatten an AndroidManifest into sorted canonical lines.

    manifest.py ARCHIVE|XMLFILE      flatten to stdout
    manifest.py --diff OLD NEW       diff two flattened files

An APK's manifest is binary XML and an AAB's is protobuf, and the two print
with completely different whitespace, attribute order and namespace prefixes,
so diffing their raw output is useless. Both are reduced here to a sorted set
of "path  attr=value" lines, which makes even an APK-vs-AAB comparison
meaningful.

The APK side is read directly by axml.py; only the protobuf side needs
bundletool. Both arrive as ElementTree nodes, so there is one flattener rather
than one per packaging.
"""
import functools
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import axml                                                  # noqa: E402

NS = re.compile(r'^\{([^}]*)\}(.*)$')

# Build-tool stamps: which SDK compiled the archive, not what it does. They
# differ between an APK and the AAB it was built from without either behaving
# differently, so they would be noise in every cross-packaging comparison.
DROP_ATTRS = {
    'android:compileSdkVersion',
    'android:compileSdkVersionCodename',
    'platformBuildVersionCode',
    'platformBuildVersionName',
}


@functools.lru_cache(maxsize=None)
def short(name):
    """The display form of a tag or attribute name, namespace resolved.

    Cached because a document draws its names from a small fixed vocabulary and
    asks for each one repeatedly — every attribute of every element, plus once
    per element to build its path — so the regex runs a few dozen times for a
    manifest rather than once per node.
    """
    m = NS.match(name)
    if not m:
        return name
    uri, local = m.groups()
    if uri == 'http://schemas.android.com/apk/res/android':
        return 'android:' + local
    return local


ANDROID_NAME = '{http://schemas.android.com/apk/res/android}name'


def attrs_of(el):
    return sorted((short(k), v) for k, v in el.attrib.items()
                  if short(k) not in DROP_ATTRS)


def ident(el):
    """What tells this element apart from its same-tag siblings.

    android:name where there is one: that is what the platform itself keys on,
    and it survives reordering. A <data> element has none, and one activity can
    carry a dozen of them — keyed by tag alone they land on one path, the diff
    keeps whichever came last, and every deep-link host, scheme and path prefix
    but that one is silently dropped. Their attributes are their identity, so
    fall back to those, sorted, which is likewise order-independent.
    """
    name = el.attrib.get(ANDROID_NAME)
    if name:
        return name
    own = ','.join('%s=%s' % kv for kv in attrs_of(el))
    if own:
        return own
    # An <intent-filter> and a <queries><intent> declare no attributes at all.
    # What tells one from the next is the set of actions and categories inside
    # it, so identify it by those; a <data> added to an existing filter then
    # shows up as the one added line it is.
    return ','.join(sorted(c.attrib[ANDROID_NAME] for c in el
                           if ANDROID_NAME in c.attrib))


def walk(el, path, out, key=''):
    here = '%s/%s%s' % (path, short(el.tag), ('[%s]' % key) if key else '')
    attrs = attrs_of(el)
    if attrs:
        for k, v in attrs:
            out.append('%s  %s=%s' % (here, k, v))
    else:
        out.append(here)
    # Only a tag that actually repeats needs disambiguating. Giving every
    # element a key would put the root <manifest>'s own attributes — the
    # versionCode among them — into the path of every line below it, so a
    # version bump would read as the whole manifest having changed.
    # Counted once, not re-counted per child: `tags.count(...)` inside the
    # loop is quadratic in the number of siblings, and an <application> with a
    # few thousand components is a quantity nothing here bounds.
    tags = Counter(short(c.tag) for c in el)
    for child in el:
        repeats = tags[short(child.tag)] > 1
        walk(child, here, out, ident(child) if repeats else '')


# How a value was encoded. A manifest attribute can arrive as a raw integer, a
# symbolic name, a numeric resource id or a symbolic resource reference,
# depending on which tool decoded it and whether the resource table was
# available. Two encodings of the same value must not read as a change.
NUMERIC = re.compile(r'^-?\d+$|^0x[0-9a-fA-F]+$')
REF_NUMERIC = re.compile(r'^@0x[0-9a-fA-F]+$')
REF_SYMBOLIC = re.compile(r'^@[\w.]*[:/][\w./]+$')


def canon(v):
    """(encoding, comparable form) for an attribute value."""
    if v is None:
        return 'absent', None
    if NUMERIC.match(v):
        return 'int', str(int(v, 0))          # 0x5b0, 0x000005b0 and 1456 agree
    if REF_NUMERIC.match(v):
        return 'ref-id', str(int(v[1:], 16))
    if REF_SYMBOLIC.match(v):
        return 'ref-name', v
    return 'name', v


# Encodings that cannot be compared without the resource table, or that are two
# renderings of one value. Seeing one of each is not evidence of a change.
INTERCHANGEABLE = {frozenset({'ref-id', 'ref-name'}),
                   frozenset({'int', 'name'})}


def same(a, b):
    ka, va = canon(a)
    kb, vb = canon(b)
    if ka == kb:
        return va == vb
    return frozenset({ka, kb}) in INTERCHANGEABLE


def split(line):
    """'path  attr=value' -> ('path  attr', 'value'); bare paths -> (line, None).

    The split starts at the two-space separator, not at the first '=' in the
    line: a path now carries the attributes that identify an unnamed sibling,
    so '=' appears on the left of the separator as well as on the right.
    """
    i = line.find('  ')
    if i < 0:
        return line, None
    j = line.find('=', i)
    return (line[:j], line[j + 1:]) if j >= 0 else (line, None)


def diff(old_path, new_path):
    """Diff two flattened manifests, reconciling encoding-only differences.

    bundletool prints enum attributes as raw integers (launchMode=2,
    configChanges=0x5b0) where a symbolic decoder prints singleTask and
    keyboard|orientation|..., and it resolves resource ids to names where
    reading the binary manifest alone can only give the id. Same value,
    different rendering; see INTERCHANGEABLE.
    """
    # UTF-8 explicitly: an android:label or a <meta-data> value is app text and
    # need not be ASCII, and the flattened files this reads were written as
    # UTF-8 by main() below. Leaving the encoding to the ambient locale makes
    # whether the tool runs a property of the environment rather than of the
    # input.
    def flat(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read().splitlines()

    return diff_flat(flat(old_path), flat(new_path))


def diff_flat(old_lines, new_lines):
    """The same comparison over already-flattened lines.

    Split out so an entry read straight from an archive never has to be spilled
    to a file first: the resource-XML pass compares hundreds of entries and
    would otherwise pay a temporary file, and a re-read, for each one.
    """
    old = dict(split(l) for l in old_lines if l)
    new = dict(split(l) for l in new_lines if l)
    out = []
    for k in sorted(set(old) | set(new)):
        missing = object()
        a, b = old.get(k, missing), new.get(k, missing)
        if a is missing or b is missing:
            if a is not missing:
                out.append('-' + (k if a is None else '%s=%s' % (k, a)))
            if b is not missing:
                out.append('+' + (k if b is None else '%s=%s' % (k, b)))
            continue
        if not same(a, b):
            out.append('-%s=%s' % (k, a))
            out.append('+%s=%s' % (k, b))
    return out


def diff_entries(old_path, new_path, rows):
    """Decode and diff many binary-XML entries, opening each archive once.

    `rows` is (reported name, entry in OLD, entry in NEW). Each archive holds
    a couple of thousand entries and is a couple of hundred megabytes, so the
    central directory is read once here rather than once per entry — the shell
    loop this replaced spawned two interpreters and re-opened both archives for
    every file, which cost 84 seconds on an apk-vs-aab pair.

    Emits the artefact stage 7 reads: `=== name` and then either the diff, a
    `~` line for an entry that decoded identically, or a `!` line for one that
    did not decode at all.
    """
    out = []
    with zipfile.ZipFile(old_path) as zo, zipfile.ZipFile(new_path) as zn:
        for name, in_old, in_new in rows:
            out.append('=== %s' % name)
            try:
                before = flatten(axml.parse(zo.read(in_old)))
                after = flatten(axml.parse(zn.read(in_new)))
            except (axml.Bad, KeyError, OSError, struct.error, ValueError):
                out.append('!could not decode as binary XML')
                continue
            # An entry whose CRC moved and whose content did not is the packer
            # re-encoding it. Said rather than dropped: stage 7 subtracts every
            # name here from the entries it reports as unexamined.
            out.extend(diff_flat(before, after) or ['~re-encoded; no content change'])
    return out


BUNDLE_MARKERS = ('BundleConfig.pb', 'base/manifest/')


def is_bundle(names):
    return any(n == BUNDLE_MARKERS[0] or n.startswith(BUNDLE_MARKERS[1])
               for n in names)


def from_bundle(path):
    """bundletool renders the protobuf manifest back to text XML."""
    try:
        out = subprocess.run(
            ['bundletool', 'dump', 'manifest', '--bundle=' + path],
            capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        raise axml.Bad('bundletool did not run')
    if not out.stdout.strip():
        raise axml.Bad('bundletool printed no manifest')
    # bundletool writes JVM warnings to stderr; stdout is the XML alone.
    return parse_xml(out.stdout.decode('utf-8', 'replace'))


def parse_xml(text):
    """Parse a manifest's XML, refusing any document that declares a DTD.

    A manifest never has one, and a DTD is how entity-expansion bombs work;
    the expat bundled with older Pythons (macOS's 3.9 has 2.2.8) predates
    expat's own amplification limit, and the input comes from an archive
    somebody else built.
    """
    if '<!DOCTYPE' in text or '<!ENTITY' in text:
        raise axml.Bad('XML declares a DTD; a manifest never does')
    return ET.fromstring(text)  # nosemgrep: use-defused-xml-parse


def root_of(path):
    """The manifest root of an APK, an AAB, or an already-decoded XML file."""
    try:
        with zipfile.ZipFile(path) as z:
            if is_bundle(z.namelist()):
                return from_bundle(path)
            if 'AndroidManifest.xml' not in z.namelist():
                raise axml.Bad('no AndroidManifest.xml')
            return axml.parse(z.read('AndroidManifest.xml'))
    except zipfile.BadZipFile:
        with open(path, encoding='utf-8', errors='replace') as f:
            return parse_xml(f.read())


def flatten(root):
    out = []
    walk(root, '', out)
    return sorted(set(out))


def main(argv):
    # Both output paths carry app text; see diff() on why this is not left to
    # the locale.
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if len(argv) > 1 and argv[1] == '--diff':
        if len(argv) != 4:
            sys.stderr.write('usage: manifest.py --diff OLD NEW\n')
            return 2
        lines = diff(argv[2], argv[3])
        if lines:
            print('\n'.join(lines))
        return 0
    if len(argv) > 1 and argv[1] == '--entries':
        if len(argv) != 5:
            sys.stderr.write('usage: manifest.py --entries OLD NEW ROWS.tsv\n')
            return 2
        try:
            with open(argv[4], encoding='utf-8') as f:
                rows = [l.rstrip('\n').split('\t')
                        for l in f if l.count('\t') == 2]
            print('\n'.join(diff_entries(argv[2], argv[3], rows)))
        except (OSError, zipfile.BadZipFile) as e:
            sys.stderr.write('cannot read: %s\n' % e)
            return 1
        return 0
    if len(argv) != 2:
        sys.stderr.write('usage: manifest.py ARCHIVE|XMLFILE\n'
                         '   or: manifest.py --diff OLD NEW\n'
                         '   or: manifest.py --entries OLD NEW ROWS.tsv\n')
        return 2
    try:
        print('\n'.join(flatten(root_of(argv[1]))))
    except (axml.Bad, OSError, ET.ParseError, struct.error) as e:
        sys.stderr.write('cannot read %s: %s\n' % (argv[1], e))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
