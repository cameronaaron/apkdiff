"""Read an Android resource table (resources.arsc) into name=value lines.

    arsc.py ARCHIVE|resources.arsc     flatten to stdout
    arsc.py --diff OLD NEW             diff two flattened files

Every other stage reads what the compiler emitted: obfuscated class names,
opcode hashes, log strings salvaged out of .rodata. The resource table is the
one part of a release build still written in the words the developer chose.
`string/call_failed_title = "Call failed"` names both the text a person sees
and what its author called it, and neither R8 nor the linker touches either.

Only the entries worth diffing come out:

* **The default configuration only.** A build carries the same string once per
  locale and once per density; comparing all of them turns one edit into forty
  lines that say the same thing. The header counts what was left behind, so a
  translation sweep is still visible as a number.

* **Text and flags only** — string, plurals, array, bool, integer. A drawable
  or layout entry's value is a file path the resource shrinker renames, or a
  reference whose numeric target renumbers whenever anything is added above it:
  churn by construction, and never a fact about the release.

* **No resource ids anywhere.** An id is (package, type, index), and the index
  is assigned in declaration order, so inserting one string shifts every id
  after it. Names are stable; ids are not.

Reference: AOSP frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h
"""
import struct
import sys
import zipfile
from collections import Counter

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import axml                                                  # noqa: E402
from axml import Bad, _u16, _u32, string_pool                # noqa: E402

RES_TABLE = 0x0002
RES_STRING_POOL = 0x0001
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201

# ResTable_type flags.
FLAG_SPARSE = 0x01
FLAG_OFFSET16 = 0x02
NO_ENTRY_32 = 0xFFFFFFFF
NO_ENTRY_16 = 0xFFFF

# ResTable_entry flags.
ENTRY_COMPLEX = 0x0001
ENTRY_COMPACT = 0x0008

# The types whose values say something about the release. See the module
# docstring for why the rest are excluded rather than merely uninteresting.
WANTED = frozenset(('string', 'plurals', 'array', 'bool', 'integer'))

ENTRY_NAME = 'resources.arsc'


def _u8(b, o):
    return b[o]


def chunks(b, start, end):
    """Walk sibling chunks, yielding (type, offset, header size, size).

    A zero or negative size would loop forever and a size past the end would
    read another chunk's bytes, so both are rejected here rather than by every
    caller.
    """
    o = start
    while o + 8 <= end:
        ctype, hsize, csize = _u16(b, o), _u16(b, o + 2), _u32(b, o + 4)
        if csize < 8 or hsize < 8 or o + csize > end:
            raise Bad('chunk at 0x%x runs past the end of its parent' % o)
        yield ctype, o, hsize, csize
        o += csize


def escape(s):
    """One value on one line, reversibly.

    A string resource may contain a newline or a tab, and the artefact this
    writes is compared line by line by everything downstream.
    """
    return (s.replace('\\', '\\\\').replace('\n', '\\n')
             .replace('\r', '\\r').replace('\t', '\\t'))


def _locale(b, o):
    """The language of a ResTable_config, as two letters, or '' for any.

    The two bytes are ASCII for an ordinary two-letter code; a three-letter
    code is packed into them with the high bit set, and is reported as its
    packed form rather than unpacked — this is used to count how many locales a
    build carries, not to name them.
    """
    lang = b[o + 8:o + 10]
    if lang == b'\0\0':
        return ''
    if lang[0] & 0x80:
        return '#%02x%02x' % (lang[0], lang[1])
    return lang.decode('ascii', 'replace')


def _value(pool, dtype, data):
    """A Res_value as text, or None when it is not worth comparing.

    None covers references, attributes and the float family: what they hold is
    a number that the build assigns, not something a person wrote.
    """
    if dtype == axml.TYPE_STRING:
        if data >= len(pool):
            raise Bad('string index %d past the end of the value pool' % data)
        return pool[data]
    if dtype == axml.TYPE_INT_BOOLEAN:
        return 'true' if data else 'false'
    if dtype in (axml.TYPE_INT_DEC, axml.TYPE_INT_HEX):
        return str(data)
    return None


def _package(b, o, hsize, csize, pool, out, meta):
    """One ResTable_package: its type and key pools, then its type chunks."""
    # header(8) id(4) name(256) typeStrings(4) lastPublicType(4) keyStrings(4)
    name = b[o + 12:o + 268].decode('utf-16-le', 'replace').split('\0', 1)[0]
    type_off, key_off = _u32(b, o + 268), _u32(b, o + 276)
    types = string_pool(b, o + type_off) if type_off else []
    keys = string_pool(b, o + key_off) if key_off else []
    for ctype, t, thsize, tcsize in chunks(b, o + hsize, o + csize):
        # RES_TABLE_TYPE_SPEC carries per-entry configuration flags and no
        # values; the library and overlay chunks describe other packages. None
        # of them holds text, so the values live entirely in RES_TABLE_TYPE.
        if ctype == RES_TABLE_TYPE:
            _type(b, t, thsize, pool, types, keys, name, out, meta)
    return name


def _type(b, t, hsize, pool, types, keys, pkg, out, meta):
    """One ResTable_type: every entry of one type in one configuration."""
    type_id = _u8(b, t + 8)
    flags = _u8(b, t + 9)
    count = _u32(b, t + 12)
    entries_start = _u32(b, t + 16)
    kind = types[type_id - 1] if 0 < type_id <= len(types) else 'type%d' % type_id
    if kind not in WANTED:
        return
    # A configuration is "default" when every field after its own size is zero.
    # Reading the fields individually would mean tracking which ones each
    # platform version added; a zero test does not care.
    config_size = _u32(b, t + 20)
    if config_size < 4 or t + 20 + config_size > t + hsize:
        raise Bad('configuration at 0x%x runs past its own chunk header' % t)
    if any(b[t + 24:t + 20 + config_size]):
        meta['nondefault'] += count
        lang = _locale(b, t + 20)
        if lang:
            meta.setdefault('locales', set()).add(lang)
        return
    for _, off in _offsets(b, t + hsize, count, flags):
        _entry(b, t + entries_start + off, pool, keys, kind, pkg, out, meta)


def _offsets(b, o, count, flags):
    """(entry index, byte offset) for the entries this chunk actually holds.

    Three encodings of the same array, all of them in the wild: a plain u32 per
    entry, a u16 per entry in units of four bytes (FLAG_OFFSET16), and a sorted
    (index, offset) pair per present entry (FLAG_SPARSE), which a type with
    few entries spread over a wide index range uses instead of a mostly-empty
    array.
    """
    if flags & FLAG_SPARSE:
        for i in range(count):
            yield _u16(b, o + 4 * i), _u16(b, o + 4 * i + 2) * 4
    elif flags & FLAG_OFFSET16:
        for i in range(count):
            off = _u16(b, o + 2 * i)
            if off != NO_ENTRY_16:
                yield i, off * 4
    else:
        for i in range(count):
            off = _u32(b, o + 4 * i)
            if off != NO_ENTRY_32:
                yield i, off


def _entry(b, p, pool, keys, kind, pkg, out, meta):
    """One ResTable_entry: a single value, or a bag of them."""
    flags = _u16(b, p + 2)
    if flags & ENTRY_COMPACT:
        # Compact entries (aapt2 --enable-compact-entries) drop the Res_value
        # header: the key is 16 bits, the value's type moves into the top byte
        # of the flags, and the data follows immediately. Eight bytes where the
        # classic form takes sixteen.
        key = _u16(b, p)
        value = _value(pool, flags >> 8, _u32(b, p + 4))
        _emit(out, meta, pkg, kind, _key(keys, key), value)
        return
    size = _u16(b, p)
    name = _key(keys, _u32(b, p + 4))
    if not flags & ENTRY_COMPLEX:
        _emit(out, meta, pkg, kind, name,
              _value(pool, _u8(b, p + size + 3), _u32(b, p + size + 4)))
        return
    # A bag: a plurals set, a string-array, or a style. Its items are numbered
    # rather than named — the name field is a reference to the attribute the
    # item satisfies, which for an array is just its position — so they are
    # reported by index, which is stable as long as the array is.
    # `size` is the length of the entry header, which for a bag already
    # includes the parent and count fields — so the items begin at p + size,
    # and adding the two fields again lands in the middle of the first one.
    count = _u32(b, p + 12)
    for i in range(count):
        item = p + size + 12 * i
        _emit(out, meta, pkg, kind, '%s[%d]' % (name, i),
              _value(pool, _u8(b, item + 7), _u32(b, item + 8)))


def _key(keys, idx):
    if idx >= len(keys):
        raise Bad('key index %d past the end of the key pool' % idx)
    return keys[idx]


def _emit(out, meta, pkg, kind, name, value):
    if value is None:
        # Counted, not dropped silently: a type this reader claims to cover
        # whose value it declined to render has to be visible somewhere, or
        # "N values" reads as a complete list when it is not.
        meta['references'] += 1
        return
    out.setdefault((pkg, '%s/%s' % (kind, name)), value)


def read(b):
    """resources.arsc bytes -> ({(package, type/name): value}, meta counters)."""
    if len(b) < 12 or _u16(b, 0) != RES_TABLE:
        raise Bad('not a resource table')
    pool, out, meta = None, {}, Counter()
    packages = []
    for ctype, o, hsize, csize in chunks(b, _u16(b, 2), min(_u32(b, 4), len(b))):
        if ctype == RES_STRING_POOL and pool is None:
            pool = string_pool(b, o)
        elif ctype == RES_TABLE_PACKAGE:
            if pool is None:
                raise Bad('a package chunk came before the value pool')
            packages.append(_package(b, o, hsize, csize, pool, out, meta))
    if pool is None:
        raise Bad('no value string pool')
    return out, meta, packages


def flatten(b):
    """The lines written to the artefact: a `#` header, then sorted values.

    The key carries its package only when the table holds more than one. A
    lone package is the app's own and naming it on every line is noise; two
    packages defining the same name are two different values, and merging them
    would report one of them as the other's change.
    """
    out, meta, packages = read(b)
    many = len(packages) > 1
    langs = sorted(meta.get('locales', ()))
    lines = ['# packages %s' % ' '.join(packages or ['(none)']),
             '# values %d' % len(out),
             '# nondefault %d' % meta['nondefault'],
             '# locales %d' % len(langs),
             # The codes themselves, not only how many: a build that starts
             # shipping a language has done something for the people who read
             # it, and a count alone cannot say which one arrived.
             '# languages %s' % ' '.join(langs),
             '# references %d' % meta['references']]
    body = {}
    for (pkg, name), value in out.items():
        body['%s:%s' % (pkg, name) if many else name] = value
    return lines + ['%s\t%s' % (k, escape(body[k])) for k in sorted(body)]


def from_archive(path):
    """The resource table inside an archive, or the file itself."""
    with open(path, 'rb') as f:
        head = f.read(4)
    if head[:2] == b'PK':
        with zipfile.ZipFile(path) as z:
            for name in (ENTRY_NAME, 'base/' + ENTRY_NAME):
                if name in z.namelist():
                    return z.read(name)
        raise Bad('no %s in %s' % (ENTRY_NAME, path))
    with open(path, 'rb') as f:
        return f.read()


# ---------------------------------------------------------------- diff

def load(path):
    """A flattened file back into {name: value}, header lines discarded."""
    out = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            name, _, value = line.partition('\t')
            out[name] = value
    return out


def diff(old, new):
    """The report: what a person reads, grouped by what happened.

    Changed values first and quoted in full. A reworded message is the single
    most useful line in this whole report — it is the app's own text, before
    and after, with the developer's name for it — and burying it under a list
    of additions would be filing it by mechanism rather than by interest.
    """
    a, b = load(old), load(new)
    changed = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
    added = sorted(b.keys() - a.keys())
    removed = sorted(a.keys() - b.keys())
    out = []
    if changed:
        out.append('--- changed values (%d)' % len(changed))
        for k in changed:
            out.append('  %s' % k)
            out.append('      was  %s' % a[k])
            out.append('      now  %s' % b[k])
        out.append('')
    for title, keys, side in (('added values', added, b),
                              ('removed values', removed, a)):
        if keys:
            out.append('--- %s (%d)' % (title, len(keys)))
            out.extend('  %s = %s' % (k, side[k]) for k in keys)
            out.append('')
    if not out:
        out.append('(the resource values are identical)')
    return out


def main(argv):
    if len(argv) == 4 and argv[1] == '--diff':
        text = diff(argv[2], argv[3])
    elif len(argv) == 2:
        text = flatten(from_archive(argv[1]))
    else:
        sys.stderr.write('usage: arsc.py ARCHIVE | --diff OLD NEW\n')
        return 2
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stdout.write('\n'.join(text) + '\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    # A resource table is a graph of offsets into itself, and a malformed one
    # reaches past the end of the buffer in whichever direction it was damaged.
    # The checks above catch the shapes worth naming; anything past them is
    # still "these bytes are not a table we can read", and the caller — a shell
    # stage that discards both sides when either fails — needs a message and a
    # status rather than a traceback.
    except (Bad, IndexError, struct.error) as exc:
        sys.stderr.write('arsc.py: %s\n' % exc)
        sys.exit(1)
