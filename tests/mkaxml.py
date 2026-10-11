"""Build a binary AndroidManifest.xml, for tests that need a readable build.

Used by both suites: the Python tests import `axml`, the shell tests run this
as a command. One implementation so the two cannot drift apart.

    python3 tests/mkaxml.py OUT.xml VERSIONCODE [PACKAGE]
"""
import struct
import sys


def _len8(n):
    """A UTF-8 pool length: one byte, or two with the high bit set."""
    return bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])


def _len16(n):
    """A UTF-16 pool length: one unit, or two with the high bit set."""
    if n < 0x8000:
        return struct.pack("<H", n)
    return struct.pack("<HH", 0x8000 | (n >> 16), n & 0xFFFF)


ANDROID_NS = "http://schemas.android.com/apk/res/android"


def _attr_bytes(attrs, idx):
    """The 20-byte records for one element's attributes."""
    body = b""
    for name, (dtype, num, raw) in attrs.items():
        ns, _, local = name.rpartition(":")
        rawidx = idx[raw] if isinstance(raw, str) else 0xFFFFFFFF
        # A TYPE_STRING attribute carries the pool index in `data` as well as
        # in `rawValue`; that is what aapt emits and what the parser reads.
        if dtype == 0x03:
            num = rawidx
        body += struct.pack("<IIIHBBI",
                            idx[ANDROID_NS] if ns else 0xFFFFFFFF,
                            idx[local], rawidx, 8, 0, dtype, num)
    return body


def _element(tag, attrs, idx):
    body = _attr_bytes(attrs, idx)
    return struct.pack("<HHIIIIIHHHHHH", 0x0102, 16, 16 + 20 + len(body),
                       1, 0xFFFFFFFF, 0xFFFFFFFF, idx[tag],
                       20, 20, len(attrs), 0, 0, 0) + body


def _end(tag, idx):
    return struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF,
                       0xFFFFFFFF, idx[tag])


ANDROID_NS = 'http://schemas.android.com/apk/res/android'


def _split(name):
    """'android:name' -> (the Android namespace URI, 'name')."""
    prefix, sep, local = name.partition(':')
    return (ANDROID_NS, local) if sep and prefix == 'android' else ('', name)


def _element(idx, tag, attrs):
    """START_ELEMENT + its attributes, then the matching END_ELEMENT."""
    body = b""
    for name, (dtype, num, raw) in attrs.items():
        rawidx = idx[raw] if isinstance(raw, str) else 0xFFFFFFFF
        # A TYPE_STRING attribute carries the pool index in `data` as well as
        # in `rawValue`; that is what aapt emits and what the parser reads.
        if dtype == 0x03:
            num = rawidx
        ns, local = _split(name)
        body += struct.pack("<IIIHBBI",
                            idx[ns] if ns else 0xFFFFFFFF, idx[local], rawidx,
                            8, 0, dtype, num)
    start = struct.pack("<HHIIIIIHHHHHH", 0x0102, 16, 16 + 20 + len(body),
                        1, 0xFFFFFFFF, 0xFFFFFFFF, idx[tag],
                        20, 20, len(attrs), 0, 0, 0) + body
    end = struct.pack("<HHIIIII", 0x0103, 16, 24, 1, 0xFFFFFFFF,
                      0xFFFFFFFF, idx[tag])
    return start, end


def axml(attrs, element="manifest", utf8=False, filler=None, extra_chunk=b"",
         children=()):
    """A minimal binary XML document carrying the given attributes.

    Built by hand rather than taken from a real APK so a test states exactly
    which bytes it depends on. attrs maps name -> (dataType, data, rawValue),
    where rawValue is a string to put in the pool or None. An attribute name
    may be written "uri|local" to place it in a namespace. children is a list
    of (tag, attrs) nested inside the root.
    """
    def names(a):
        out = []
        for k in a:
            ns, local = _split(k)
            out += ([ns] if ns else []) + [local]
        return out

    strings = [element] + names(attrs)
    strings += [v[2] for v in attrs.values() if isinstance(v[2], str)]
    for tag, ca in children:
        strings += [tag] + names(ca)
        strings += [v[2] for v in ca.values() if isinstance(v[2], str)]
    if filler:                     # to push a pool entry into the long-length form
        strings.append(filler)
    seen = []
    for s in strings:              # keep first occurrence, drop duplicates
        if s not in seen:
            seen.append(s)
    strings = seen
    idx = {s: i for i, s in enumerate(strings)}

    data = b""
    offsets = []
    for s in strings:
        offsets.append(len(data))
        if utf8:
            b = s.encode("utf-8")
            data += _len8(len(s)) + _len8(len(b)) + b + b"\x00"
        else:
            data += _len16(len(s)) + s.encode("utf-16-le") + b"\x00\x00"
    while len(data) % 4:
        data += b"\x00"
    strings_start = 28 + 4 * len(strings)
    pool = struct.pack(
        "<HHIIIIII", 0x0001, 28, strings_start + len(data),
        len(strings), 0, (1 << 8) if utf8 else 0, strings_start, 0,
    ) + b"".join(struct.pack("<I", o) for o in offsets) + data

    start, end = _element(idx, element, attrs)
    inner = b""
    for tag, ca in children:
        cs, ce = _element(idx, tag, ca)
        inner += cs + ce
    body = pool + extra_chunk + start + inner + end
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def main(argv):
    # A resource XML document rather than a manifest. res/xml/ entries are the
    # same AXML format, and stage 2 decodes them with the same reader, so the
    # suite needs to be able to build one. Shaped after the file_paths.xml a
    # FileProvider declares, which is where this mattered in practice.
    if len(argv) > 1 and argv[1] == "--paths":
        if len(argv) not in (4, 5):
            sys.stderr.write("usage: mkaxml.py --paths OUT.xml PATH [--utf8]\n")
            return 2
        # --utf8 writes the same document with a UTF-8 string pool instead of a
        # UTF-16 one: different bytes, identical content. aapt2 chooses between
        # the two, so this is what a re-encoded resource actually looks like.
        with open(argv[2], "wb") as f:
            f.write(axml({}, element="paths", utf8=(argv[-1] == "--utf8"),
                         children=[
                ("files-path", {"name": (0x03, 0, "attachments"),
                                "path": (0x03, 0, argv[3])}),
            ]))
        return 0
    if len(argv) < 3:
        sys.stderr.write("usage: mkaxml.py OUT.xml VERSIONCODE [PACKAGE]\n"
                         "   or: mkaxml.py --paths OUT.xml PATH\n")
        return 2
    pkg = argv[3] if len(argv) > 3 else "com.example.app"
    with open(argv[1], "wb") as f:
        f.write(axml({
            "package": (0x03, 0, pkg),
            "versionCode": (0x10, int(argv[2]), None),
            "versionName": (0x03, 0, "1.0"),
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
