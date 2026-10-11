"""Read Android's binary XML (AXML) into ElementTree nodes.

An APK's AndroidManifest.xml is a compiled chunk format, not text. The usual
way to read it is to hand the archive to apktool, which starts a JVM, decodes
the whole thing and writes text XML — about a second and a half per build, and
it moves versionCode, versionName and the sdk versions out of the manifest into
its own apktool.yml on the way, so the decoded manifest is missing exactly the
fields most worth diffing.

This is a small, well-specified format, so it is parsed here instead. Nodes come
back as xml.etree Elements with `{uri}local` attribute keys, which is the same
shape ElementTree produces for real XML — so one flattener serves both an APK
read here and an AAB rendered to text by bundletool.

Reference: AOSP frameworks/base/include/androidfw/ResourceTypes.h
"""
import struct
import xml.etree.ElementTree as ET

RES_XML = 0x0003
RES_STRING_POOL = 0x0001
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103
UTF8_FLAG = 1 << 8

# Res_value types. Only the ones a manifest actually uses are rendered by name;
# anything else falls through to hex, which still diffs correctly because both
# sides are rendered the same way.
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_FLOAT = 0x04
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

ANDROID_NS = 'http://schemas.android.com/apk/res/android'


class Bad(Exception):
    """The bytes are not the AXML we expected. Callers fall back."""


def _u16(b, o):
    return struct.unpack_from('<H', b, o)[0]


def _u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def _varint(b, o, wide):
    """Pool lengths are 1 or 2 units, with the high bit marking a long form."""
    if wide:
        n = _u16(b, o)
        if n & 0x8000:
            return ((n & 0x7FFF) << 16) | _u16(b, o + 2), o + 4
        return n, o + 2
    n = b[o]
    if n & 0x80:
        return ((n & 0x7F) << 8) | b[o + 1], o + 2
    return n, o + 1


def string_pool(b, o):
    """Decode a RES_STRING_POOL chunk at offset o into a list of str."""
    count = _u32(b, o + 8)
    flags = _u32(b, o + 16)
    start = _u32(b, o + 20)
    utf8 = bool(flags & UTF8_FLAG)
    out = []
    for i in range(count):
        p = o + start + _u32(b, o + 28 + 4 * i)
        if utf8:
            _, p = _varint(b, p, False)          # length in characters
            n, p = _varint(b, p, False)          # length in bytes
            out.append(b[p:p + n].decode('utf-8', 'replace'))
        else:
            n, p = _varint(b, p, True)
            out.append(b[p:p + 2 * n].decode('utf-16-le', 'replace'))
    return out


def _pooled(pool, idx):
    """A pool index that is out of range means a malformed file, not a crash."""
    if idx == 0xFFFFFFFF:
        return None
    if idx >= len(pool):
        raise Bad('string index %d past the end of the pool' % idx)
    return pool[idx]


def render(pool, dtype, data, raw):
    """A Res_value as the text a decoder would print for it."""
    if dtype == TYPE_STRING:
        return _pooled(pool, data) or ''
    if dtype == TYPE_INT_BOOLEAN:
        return 'true' if data else 'false'
    # Decimal and hex are two spellings of one integer. Rendering both in
    # decimal means a value written 0x5b0 in one build and 1456 in the other
    # does not read as a change, and that a versionCode is always a number the
    # rest of the tool can sort and use as a filename.
    if dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
        return str(data)
    if dtype == TYPE_REFERENCE:
        return '@0x%08x' % data
    if dtype == TYPE_FLOAT:
        return repr(struct.unpack('<f', struct.pack('<I', data))[0])
    # Anything else keeps its raw string if it has one, else its hex value.
    return raw if raw is not None else '0x%08x' % data


def parse(b):
    """Binary XML bytes -> the root Element.

    Elements nest exactly as the START/END pairs do; a document that closes an
    element it never opened is rejected rather than silently reparented.
    """
    if len(b) < 8 or _u16(b, 0) != RES_XML:
        raise Bad('not a binary XML chunk')
    pool, root, stack = None, None, []
    o, end = _u16(b, 2), min(_u32(b, 4), len(b))
    while o + 8 <= end:
        ctype, hsize, csize = _u16(b, o), _u16(b, o + 2), _u32(b, o + 4)
        if csize < 8 or o + csize > end:
            raise Bad('chunk runs past the end')
        if ctype == RES_STRING_POOL:
            pool = string_pool(b, o)
        elif pool is None:
            raise Bad('element before the string pool')
        elif ctype == RES_XML_START_ELEMENT:
            el = _element(b, o, hsize, pool)
            if stack:
                stack[-1].append(el)
            elif root is None:
                root = el
            stack.append(el)
        elif ctype == RES_XML_END_ELEMENT:
            if not stack:
                raise Bad('element closed but never opened')
            stack.pop()
        # Every other chunk — the namespace declarations, the resource map — is
        # skipped: the attribute keys already carry their namespace URI, so
        # nothing here needs the prefix table.
        o += csize
    if root is None:
        raise Bad('no elements')
    return root


def _element(b, o, hsize, pool):
    ns = _pooled(pool, _u32(b, o + hsize))
    name = _pooled(pool, _u32(b, o + hsize + 4))
    el = ET.Element('{%s}%s' % (ns, name) if ns else name)
    astart = _u16(b, o + hsize + 8)
    asize = _u16(b, o + hsize + 10)
    acount = _u16(b, o + hsize + 12)
    for i in range(acount):
        a = o + hsize + astart + i * asize
        ans = _pooled(pool, _u32(b, a))
        key = _pooled(pool, _u32(b, a + 4))
        raw = _pooled(pool, _u32(b, a + 8))
        dtype = b[a + 15]
        el.set('{%s}%s' % (ans, key) if ans else key,
               render(pool, dtype, _u32(b, a + 16), raw))
    return el


def manifest_attrs(b):
    """The <manifest> element's own attributes, as a plain {name: value} dict.

    A convenience for callers that only want package and version: the android:
    prefix is stripped so `versionCode` reads the same whether or not the file
    declared the namespace.
    """
    root = parse(b)
    if root.tag != 'manifest':
        raise Bad('root element is <%s>, not <manifest>' % root.tag)
    out = {}
    for k, v in root.attrib.items():
        out[k.rsplit('}', 1)[-1]] = v
    return out
