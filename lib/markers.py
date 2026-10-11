import re, sys, zipfile

# Rust: todo!() panics with "not yet implemented", unimplemented!() with
# "not implemented". Kotlin: TODO() compiles to NotImplementedError, whose
# message is inlined as "An operation is not implemented: <reason>" — the
# literal word TODO does not survive, so match the message instead.
PATS = [rb'TODO', rb'FIXME', rb'XXX:', rb'HACK',
        rb'not yet implemented', rb'An operation is not implemented']

for apk in sys.argv[1:]:
    print("=" * 70)
    print(apk)
    print("=" * 70)
    with zipfile.ZipFile(apk) as z:
        for info in z.infolist():
            if info.file_size > 200 * 1024 * 1024:
                continue
            data = z.read(info)
            for pat in PATS:
                for m in re.finditer(pat, data):
                    i = m.start()
                    seg = data[max(0, i - 130): i + 130]
                    ctx = re.sub(rb'[^\x20-\x7e]{1,}', b' | ', seg).decode('ascii', 'replace')
                    print("  [%s] %s @0x%x" % (pat.decode(), info.filename, i))
                    print("      %s" % ctx)
    print()
