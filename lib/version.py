"""Identify a build by what is inside it, never by what it is called.

    version.py FILE  ->  format \t package \t versionCode \t versionName

Builds arrive as 20002242.apk_, base.aab, download (3).apk. The name is the
least reliable thing about one, so the format comes from the archive's layout
and the version from its manifest.
"""
import struct
import sys
import zipfile

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import axml                                                  # noqa: E402
import manifest                                              # noqa: E402

FIELDS = ('package', 'versionCode', 'versionName')


def describe(path):
    """(format, {package, versionCode, versionName})."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if manifest.is_bundle(names):
            root = manifest.from_bundle(path)
            attrs = {k.rsplit('}', 1)[-1]: v for k, v in root.attrib.items()}
            return 'aab', attrs
        if 'AndroidManifest.xml' not in names:
            raise axml.Bad('no AndroidManifest.xml')
        return 'apk', axml.manifest_attrs(z.read('AndroidManifest.xml'))


def main(argv):
    # versionName is a free-text field the developer writes; it is not
    # guaranteed ASCII, and this line is read by the shell to name a file.
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if len(argv) != 2:
        sys.stderr.write('usage: version.py FILE\n')
        return 2
    try:
        fmt, a = describe(argv[1])
    except (axml.Bad, OSError, zipfile.BadZipFile, struct.error) as e:
        sys.stderr.write('cannot read %s: %s\n' % (argv[1], e))
        return 1
    print('\t'.join([fmt] + [a.get(k, '') for k in FIELDS]))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
