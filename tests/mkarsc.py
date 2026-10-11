"""Build a resource table (resources.arsc) for the tests.

The single arsc fixture builder, imported by the Python suite and run as a
command by the shell suite, for the same reason `mkaxml.py` is: two builders
drift, and a reader tested against a fixture only its own suite can produce is
tested against itself.

Everything is expressed as arguments rather than baked in, because the shapes
that matter are exactly the ones a real build varies: which offset encoding a
type chunk uses, whether an entry is compact, whether a configuration is the
default one. Those are the branches the reader has to get right.

    mkarsc.py OUT.arsc [--sparse|--offset16|--compact] [--locale xx]

Reference: AOSP frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h
"""
import struct
import sys

RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC = 0x0202

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_BOOLEAN = 0x12

UTF8_FLAG = 1 << 8
FLAG_SPARSE = 0x01
FLAG_OFFSET16 = 0x02
ENTRY_COMPLEX = 0x0001
ENTRY_COMPACT = 0x0008

CONFIG_SIZE = 56
PACKAGE_HEADER = 284          # 8 + id + 128 UTF-16 name units + four offsets


def varint8(n):
    return bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])


def string_pool(strings):
    """A UTF-8 RES_STRING_POOL, which is what aapt2 emits."""
    data, offsets = b'', []
    for s in strings:
        offsets.append(len(data))
        raw = s.encode('utf-8')
        data += varint8(len(s)) + varint8(len(raw)) + raw + b'\0'
    data += b'\0' * (-len(data) % 4)
    # The header is 28 bytes — the generic 8 plus five words — and
    # stringsStart is measured from the start of the chunk, so it counts them
    # all as well as the offset array.
    header = struct.pack('<IIIII', len(strings), 0, UTF8_FLAG,
                         28 + 4 * len(strings), 0)
    body = b''.join(struct.pack('<I', o) for o in offsets) + data
    return struct.pack('<HHI', RES_STRING_POOL, 28,
                       28 + len(body)) + header + body


def value(kind, data):
    """A Res_value: size, padding, type, data."""
    return struct.pack('<HBBI', 8, 0, kind, data)


def simple_entry(key, kind, data):
    return struct.pack('<HHI', 8, 0, key) + value(kind, data)


def compact_entry(key, kind, data):
    """The 8-byte form: the key is 16 bits and the type rides in the flags."""
    return struct.pack('<HHI', key, ENTRY_COMPACT | (kind << 8), data)


def bag_entry(key, items):
    """A map entry — a string-array or a plurals set — and its items."""
    body = struct.pack('<HHIII', 16, ENTRY_COMPLEX, key, 0, len(items))
    for i, (kind, data) in enumerate(items):
        body += struct.pack('<I', i + 1) + value(kind, data)
    return body


def type_chunk(type_id, entries, flags=0, locale=None, config_size=None,
               density=0):
    """One ResTable_type: the entries of one type in one configuration.

    `entries` is {index: bytes}. The three offset encodings are the branch this
    exists to exercise, so which one is used is the caller's choice rather than
    a function of the data.
    """
    config = bytearray(CONFIG_SIZE)
    # The declared size is a parameter because a table can lie about it, and
    # the reader has to reject that rather than read past its own header.
    config[0:4] = struct.pack('<I', CONFIG_SIZE if config_size is None
                              else config_size)
    if locale:
        # A three-letter code is packed into the same two bytes with the high
        # bit set; the caller passes the packed bytes to exercise that.
        config[8:10] = (locale if isinstance(locale, bytes)
                        else locale.encode('ascii'))
    if density:
        config[14:16] = struct.pack('<H', density)
    order = sorted(entries)
    blob, offsets = b'', {}
    for i in order:
        offsets[i] = len(blob)
        blob += entries[i]
    count = (len(order) if flags & FLAG_SPARSE else
             (max(order) + 1 if order else 0))
    if flags & FLAG_SPARSE:
        table = b''.join(struct.pack('<HH', i, offsets[i] // 4) for i in order)
    elif flags & FLAG_OFFSET16:
        table = b''.join(struct.pack('<H', offsets[i] // 4 if i in offsets
                                     else 0xFFFF) for i in range(count))
    else:
        table = b''.join(struct.pack('<I', offsets.get(i, 0xFFFFFFFF))
                         for i in range(count))
    table += b'\0' * (-len(table) % 4)
    header = (struct.pack('<BBHII', type_id, flags, 0, count,
                          20 + len(config) + len(table)) + bytes(config))
    return struct.pack('<HHI', RES_TABLE_TYPE, 8 + len(header),
                       8 + len(header) + len(table) + len(blob)) \
        + header + table + blob


def package(name, types, keys, type_chunks, pkg_id=0x7F):
    """A ResTable_package: its two pools, then its type chunks."""
    type_pool = string_pool(types)
    key_pool = string_pool(keys)
    header = (struct.pack('<I', pkg_id)
              + name.encode('utf-16-le').ljust(256, b'\0')[:256]
              + struct.pack('<IIII', PACKAGE_HEADER, 0,
                            PACKAGE_HEADER + len(type_pool), 0))
    body = type_pool + key_pool + b''.join(type_chunks)
    return struct.pack('<HHI', RES_TABLE_PACKAGE, PACKAGE_HEADER,
                       PACKAGE_HEADER + len(body)) + header + body


def table(values, packages):
    """The whole file: the value pool, then one chunk per package."""
    body = string_pool(values) + b''.join(packages)
    return struct.pack('<HHII', RES_TABLE, 12, 12 + len(body),
                       len(packages)) + body


# ---------------------------------------------------------------- the default

TYPES = ['string', 'bool', 'array', 'drawable', 'integer']
KEYS = ['app_name', 'greeting', 'wrapped', 'feature_on', 'flavours',
        'icon', 'retry_limit']
VALUES = ['Example', 'Hello, world', 'line one\nline two', 'apple', 'pear',
          'res/ic.png']


def sample(flags=0, compact=False, locale_entry=True, packages=1,
           locale='fr', density_entry=False):
    """A table with one of everything the reader claims to handle."""
    def entry(key, kind, data):
        return (compact_entry(key, kind, data) if compact
                else simple_entry(key, kind, data))

    strings = type_chunk(1, {0: entry(0, TYPE_STRING, 0),
                             1: entry(1, TYPE_STRING, 1),
                             3: entry(2, TYPE_STRING, 2)}, flags=flags)
    bools = type_chunk(2, {0: entry(3, TYPE_INT_BOOLEAN, 1)})
    # A bag and a reference-valued entry: one is text the reader must reach
    # through an indirection, the other is a value it must decline to compare.
    arrays = type_chunk(3, {0: bag_entry(4, [(TYPE_STRING, 3),
                                             (TYPE_STRING, 4),
                                             (TYPE_REFERENCE, 0x7F010000)])})
    drawables = type_chunk(4, {0: simple_entry(5, TYPE_STRING, 5)})
    integers = type_chunk(5, {0: simple_entry(6, TYPE_INT_DEC, 3)})
    chunks = [strings, bools, arrays, drawables, integers]
    if locale_entry:
        chunks.append(type_chunk(1, {0: simple_entry(1, TYPE_STRING, 1)},
                                 locale=locale))
    if density_entry:
        # A configuration that is not the default and carries no language: a
        # density, a screen size, a night-mode variant.
        chunks.append(type_chunk(1, {0: simple_entry(1, TYPE_STRING, 1)},
                                 density=320))
    pkgs = [package('com.example', TYPES, KEYS, chunks)]
    for i in range(1, packages):
        pkgs.append(package('com.other%d' % i, TYPES, KEYS,
                            [type_chunk(1, {0: simple_entry(0, TYPE_STRING, 3)})],
                            pkg_id=0x10 + i))
    return table(VALUES, pkgs)


def main(argv):
    flags, compact, locale = 0, False, True
    for a in argv[2:]:
        if a == '--sparse':
            flags |= FLAG_SPARSE
        elif a == '--offset16':
            flags |= FLAG_OFFSET16
        elif a == '--compact':
            compact = True
        elif a == '--no-locale':
            locale = False
    with open(argv[1], 'wb') as f:
        f.write(sample(flags=flags, compact=compact, locale_entry=locale))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
