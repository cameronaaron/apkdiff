"""Triage: of everything the earlier stages found, what looks like a change?

    findings.py WORKDIR REPORTDIR

Reads the machine-readable artefacts the stages leave in WORKDIR and writes a
ranked report. Each rule below is a detector: it takes the loaded evidence and
returns zero or more findings. Adding a signal means adding a detector to
DETECTORS, not editing a growing procedure.

The hard part is not finding differences — a Kotlin/Compose build has tens of
thousands between any two versions — but discarding the ones the compiler
invented. Everything here is about that.
"""
import functools
import glob
import mmap
import os
import re
import sys
from collections import Counter, namedtuple

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import arsc                                                  # noqa: E402

Finding = namedtuple('Finding', 'rank kind title evidence')

# ---------------------------------------------------------------- vocabulary

# A new failure-path log line is the single most reliable fingerprint of a bug
# fix in a stripped binary: it means code that previously fell through silently
# now detects and reports a condition.
FAIL = re.compile(r'\b(fail|error|could ?not|couldn.t|unable|cannot|invalid|'
                  r'timeout|timed out|retry|retrying|reconnect|drop|dropped|lost|'
                  r'missing|abort|panic|crash|ignor|unexpected|stale|leak|'
                  r'not initialized|no longer|gave up|giving up)', re.I)
# Stub markers: their disappearance means someone implemented the thing.
STUB = re.compile(r'\b(TODO|FIXME|XXX|HACK|unimplemented|not implemented|'
                  r'not yet implemented|stub|placeholder)\b', re.I)
# JNI descriptors, e.g. callFailed()V. Shape alone does NOT make one an upcall:
# Kotlin metadata, reflection and ordinary suspend functions all emit strings of
# exactly this form. See called_from_native.
JNI = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\((\[?[BCDFIJSZ]|\[?L[\w/$]+;)*\)'
                 r'(\[?[BCDFIJSZV]|\[?L[\w/$]+;)$')

# Classes the compiler wrote. They renumber wholesale whenever any composable
# above them is edited, so they are noise on every build.
GENERATED = re.compile(r'ComposableSingletons|\$\$ExternalSyntheticLambda|'
                       r'\$lambda[-$]|\$\$inlined')
# Members every Kotlin class has, plus the R8 lambda accessors it names after a
# hash of the body, so they churn on every build regardless of intent.
# componentN/copy/copy$default are the data-class set: the compiler writes one
# per constructor property, so a class with five fields reports eleven "new
# methods" that restate what NEW STATE already lists field by field.
# $stable is the Compose compiler's own stability marker: it is written onto
# every class the compiler touches, so a new class always "gains" one and it is
# never something a person added.
#
# The data-class set repeats itself in two spellings. Kotlin mangles the name of
# any member whose signature mentions an inline value class — `copy-ECKN2tM`,
# `equals-impl0`, `component1-xyz` — and the hash is of the erased signature, so
# it changes whenever any parameter's type does. A rule matching only the plain
# spelling reported eleven "new methods" on a data class that gained one field.
#
# access$… is the bridge R8 writes so an outer class can reach a private
# member; it exists because of the member, changes with it, and is never
# written by a person. Listing it beside the member it wraps reports one edit
# twice, once under a name nobody typed.
PLUMBING = re.compile(r'\.field [^:]*\$stable:'
                      r'|\.method [^(]*\b(invoke|invokeSuspend|create|emit|'
                      r'getKey|equals|hashCode|toString|'
                      r'component\d+|copy)(-[\w$]+|-impl\d*)?(\$default)?\('
                      r'|\.method [^(]*\$r8\$lambda\$'
                      r'|\.method [^(]*\baccess\$')
# ACC_SYNTHETIC means, by definition, "not present in the source": the bridge
# for a default argument, the DefaultConstructorMarker overload, the accessor
# an inner class reaches its outer through. A tool whose whole purpose is to
# discard compiler-invented difference has no business listing them beside the
# members somebody wrote — and they arrive in swarms, three per constructor
# that gained a parameter.
SYNTHETIC = re.compile(r'\.method [^(]*\bsynthetic\b')
# What Kotlin regenerates for a data class whenever its properties change.
DATA_CLASS = re.compile(r'\.method [^(]*\b(equals|hashCode|toString)'
                        r'(-impl\d*)?\(')
# Kotlin @Metadata signature blobs and Compose source-line markers: both churn
# on every build without meaning anything.
NOISE_STR = re.compile(r'\\u00|\.kt:\d+|^"L[\w/$;]+"$')

# The four kinds of thing the system can start. A component appearing is a new
# screen, a new background service or a new broadcast the app now answers.
# The [key] is optional: the flattener only adds one where the tag repeats.
COMPONENT = re.compile(r'/(activity|activity-alias|service|receiver|provider)'
                       r'(?:\[[^]]*\])?  android:name=(\S+)')
# <data> under an intent-filter: the URLs the app now claims from the browser.
DEEPLINK = re.compile(r'/intent-filter[^ ]*/data[^ ]*  '
                      r'android:(scheme|host|port|path\w*|mimeType)=(\S+)')
# Manifest lines some other detector has already reported in a better shape.
CLAIMED = re.compile(r'versionCode|versionName|uses-permission|'
                     r'platformBuildVersion')

# Archive entries stage 3 and stage 4 read for themselves.
CODE_ENTRY = re.compile(r'\.dex$|\.so$|(^|/)AndroidManifest\.xml$')
# Entries the build system rewrites whenever anything else changes: the
# signature blocks cover every other file, and the baseline profile is a list
# of methods to compile ahead of time, so it moves with any dex edit.
DERIVED_ENTRY = re.compile(r'^META-INF/|baseline\.prof|\.profm$|'
                           r'stamp-cert|^BUNDLE-METADATA/|^BundleConfig\.pb$')


# Content words. Four characters is long enough to skip articles and glue and
# short enough to keep "call", "lost", "peer".
WORD = re.compile(r'[a-z]{4,}')
# Words that turn up in so many unrelated messages that sharing one is no
# evidence the two are about the same thing.
STOPWORDS = frozenset(('call', 'this', 'that', 'with', 'from', 'into',
                       'have', 'been'))


def words(s):
    return set(WORD.findall(s.lower()))


def concepts(s):
    return words(s) - STOPWORDS


class TokenIndex:
    """Which strings in a corpus share vocabulary with a given one.

    Two rules below pair a string on one side of the build against strings on
    the other that share at least two content words. Asked directly that is a
    product — every native string against every new dex literal, both token
    sets recomputed on each comparison — and a real build's dex side runs to
    tens of thousands of new literals. Tokenising the corpus once into a
    postings list turns each query into a walk over only the strings that share
    a word with it.
    """

    def __init__(self, items, tokenise):
        self.items = list(items)
        self.postings = {}
        for i, s in enumerate(self.items):
            for t in tokenise(s):
                self.postings.setdefault(t, []).append(i)

    def matches(self, tokens, least=2):
        """The items sharing at least `least` tokens, in corpus order."""
        hits = Counter()
        for t in tokens:
            for i in self.postings.get(t, ()):
                hits[i] += 1
        return [self.items[i] for i in sorted(hits) if hits[i] >= least]


@functools.lru_cache(maxsize=None)
def canon(sig):
    """A signature with its digits masked, for cancelling renumbered members.

    Cached because the same signature is masked several times over: once to
    decide whether it is a renumbering of its opposite, and again to build the
    key that cancels members which merely moved between renumbered classes.
    """
    return re.sub(r'\d+', '#', sig)


ANON_SEGMENT = re.compile(r'^\d+$')


def anonymous(cls):
    """Is this a class the compiler named rather than a person?

    Kotlin and R8 name anonymous classes Outer$method$1, Outer$method$1$2 — a
    numeric segment is the tell. A nested class someone actually wrote
    (ProfileStore$StoredOffer) has no numeric segment and is kept: it is as much
    a real class as its outer one.
    """
    return any(ANON_SEGMENT.match(seg) for seg in short(cls).split('$')[1:])


def outer(cls):
    """The top-level class a generated inner class belongs to.

    A new dialog string is loaded by ComposableSingletons$CallScreenKt$
    lambda-2$1; the useful name is CallScreenKt.
    """
    tail = short(cls)
    # The ComposableSingletons$ wrapper has to come off before splitting, or the
    # "outer" class is the wrapper's own name rather than the file it belongs to.
    tail = tail[len('ComposableSingletons$'):] if tail.startswith('ComposableSingletons$') else tail
    return tail.split('$', 1)[0] or short(cls)


def short(cls):
    return cls.rsplit('/', 1)[-1]


# One type descriptor: an optional array prefix, then a primitive or Lpath;.
DESCRIPTOR = re.compile(r'\[*(?:[BCDFIJSZV]|L[^;]*;)')
PRIMITIVE = {'B': 'byte', 'C': 'char', 'D': 'double', 'F': 'float',
             'I': 'int', 'J': 'long', 'S': 'short', 'Z': 'boolean',
             'V': 'void'}


def type_name(d):
    """A Dalvik type descriptor as a person would write the type."""
    array = ''
    while d.startswith('['):
        array += '[]'
        d = d[1:]
    if d.startswith('L'):
        return short(d[1:].rstrip(';')).replace('$', '.') + array
    return PRIMITIVE.get(d, d) + array


def pretty(sig, owner=''):
    """A smali declaration rendered as a signature rather than a descriptor.

    The reports carry lines like

        .method private static final AudioCall(Lcom/x/CallActivity;
        Lcom/x/InCallService;Lcom/x/CallParty;Ljava/lang/String;
        Landroidx/compose/runtime/Composer;I)V

    which is 250 characters of package path around six words of meaning. Every
    part of it that a reader needs is in

        private static final AudioCall(CallActivity,
        InCallService, CallParty, String, Composer, int)

    and the full descriptor is still in the stage's own report for anyone who
    needs to tell two overloads apart.
    """
    owner = (short(owner) + '.') if owner else ''

    def declare(decl, tail):
        """'public final name' + the rest, with the class in front of the name."""
        flags, _, name = decl.rpartition(' ')
        return '%s%s%s%s' % (flags + ' ' if flags else '', owner, name, tail)

    if sig.startswith('.field '):
        decl, _, kind = sig[len('.field '):].rpartition(':')
        return declare(decl, ': ' + type_name(kind)) if decl else sig
    body = sig[len('.method '):] if sig.startswith('.method ') else sig
    open_paren, close_paren = body.find('('), body.rfind(')')
    if open_paren < 0 or close_paren < open_paren:
        return body
    params = ', '.join(type_name(d)
                       for d in DESCRIPTOR.findall(body[open_paren + 1:close_paren]))
    returns = type_name(body[close_paren + 1:])
    return declare(body[:open_paren],
                   '(%s)%s' % (params, '' if returns == 'void'
                               else ' -> ' + returns))


# ---------------------------------------------------------------- evidence

class Evidence:
    """Everything the stages left behind, loaded once.

    Files are optional throughout: a run where nothing native changed has no
    native artefacts, and that is not an error.
    """

    def __init__(self, work, report):
        self.work = work
        self.report = report
        self._files = {}
        self.mixed = os.path.exists(os.path.join(work, 'packaging.txt'))
        # Stage 3's apktool fallback leaves this behind. It cannot compute an
        # opcode-sequence hash from body text, so it emits no `~` rows, and
        # `rewritten` below would otherwise be silent for the one reason a
        # detector must never be silent: nothing looked.
        self.no_body_diff = os.path.exists(os.path.join(work, 'no-body-diff.txt'))
        # Stage 2's decode of the binary XML that is not the manifest, as
        # (entry, diff lines) pairs. Kept in its own artefact rather than in
        # 2-manifest.txt, whose every +/- line is reported as a manifest change.
        self.axml = []
        for l in self.lines(os.path.join(work, 'axml.diff')):
            if l.startswith('=== '):
                self.axml.append((l[4:], []))
            elif self.axml:
                self.axml[-1][1].append(l)
        self.manifest = self.lines(os.path.join(report, '2-manifest.txt'))
        self.native_added = self.lines(os.path.join(work, 'native.added.txt'))
        self.native_removed = self.lines(os.path.join(work, 'native.removed.txt'))
        # Stage 4's exported-symbol lists, and stage 1's entry inventory. Both
        # were being written and then read by nobody.
        self.sym_old = set(self.lines(os.path.join(work, 'sym.old')))
        self.sym_new = set(self.lines(os.path.join(work, 'sym.new')))
        self.entries = {st: self.lines(os.path.join(work, st + '.txt'))
                        for st in ('added', 'removed', 'changed')}
        # Stage 2's resource-table flattening, when the table changed at all.
        # Both sides or neither: a comparison against a missing side would
        # report every value the build has as new.
        self.res = {}
        self.res_meta = {}
        for side in ('old', 'new'):
            path = os.path.join(work, 'res.' + side)
            self.res[side] = arsc.load(path) if os.path.exists(path) else None
            self.res_meta[side] = {
                k: v for k, _, v in
                (l[2:].partition(' ') for l in self.lines(path)
                 if l.startswith('# '))}
        self.res_compared = None not in self.res.values()
        # The app's own code namespace, written by stage 2 from the
        # <application> class. A build bundles far more third-party code than
        # app code, and AndroidX/Compose/jsoup churn on their own schedule.
        self.ns = self.text(os.path.join(work, 'appns.txt')).strip()
        # Who this build is, for the report's first line. Taken from the new
        # side's flattened manifest, which states them whether or not they
        # changed — the manifest *diff* only names what moved.
        flat = self.lines(os.path.join(work, 'manifest.new.flat'))
        self.about = {}
        for key in ('package', 'android:versionName', 'android:versionCode'):
            for l in flat:
                head, _, value = l.partition('=')
                if head == '/manifest  ' + key:
                    self.about[key.replace('android:', '')] = value
                    break
        self.total_entries = len(self.lines(os.path.join(work, 'names.new')))
        # The stem each dex artefact shares, taken from whichever of them
        # exist: keying off one file would make the whole stage silent if that
        # one were absent.
        stems = set()
        for pattern, cut in (('*.norm', 5), ('*.api', 4), ('*.str.old', 8)):
            for path in glob.glob(os.path.join(work, pattern)):
                stems.add(os.path.basename(path)[:-cut])
        self.dexes = sorted(stems)
        self._native_maps = {}
        # Parsed member rows per dex, filled in by _members on first use, and
        # the renamed-signature pairs derived from them.
        self.member_rows = {}
        self.pairs = {}

    def text(self, path):
        """The file's contents, read at most once.

        Several detectors want the same artefact — the string pools are read by
        three of them — and the pools run to a couple of megabytes each. Reading
        per detector made the stage's cost a function of how many rules exist
        rather than of how much the build changed.
        """
        if path not in self._files:
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    self._files[path] = f.read()
            except OSError:
                self._files[path] = ''
        return self._files[path]

    def lines(self, path):
        return [l.rstrip('\n') for l in self.text(path).splitlines() if l.strip()]

    def dex(self, name, suffix):
        return self.lines(os.path.join(self.work, '%s.%s' % (name, suffix)))

    def is_app(self, cls):
        """True when the class is the app's own. Without a namespace everything
        is: reporting nothing is worse than reporting a dependency's change."""
        return not self.ns or cls.startswith(self.ns + '/')

    def split_app(self, items, key=lambda x: x[0]):
        """(the app's own items, count of everything else)."""
        own = [i for i in items if self.is_app(key(i))]
        return own, len(items) - len(own)

    def called_from_native(self, desc):
        """Does the native library ask for this method by name?

        A descriptor-shaped string in the dex is not by itself an upcall; what
        makes it one is GetMethodID(cls, name, sig) on the other side. Stage 4
        extracts the changed .so files to work/new/lib/; if none are there, no
        library changed, and a byte-identical library cannot have gained a
        callback — so the answer is no without reading anything.
        """
        return self.named_by_native(desc.split('(', 1)[0])

    def named_by_native(self, needle, side='new'):
        """Does one side's library contain this text?

        The question every cross-layer rule reduces to. `GetMethodID(cls, name,
        sig)` takes both halves as C strings, so a method the native side calls
        has its name — and usually its descriptor — sitting in the library's
        .rodata whether or not either appears in the dex.
        """
        # mmap.find, not `in`: a mapping is a sequence of ints, so `bytes in
        # mapping` is an element test that silently answers False for every
        # multi-byte needle.
        raw = needle.encode()
        return any(m.find(raw) >= 0 for m in self._native_haystacks(side))

    def _native_haystacks(self, side='new'):
        """One side's changed libraries, mapped once and searched in place.

        Each is tens of megabytes and there is one per ABI, so reading them per
        candidate made the check quadratic in a way nothing else here is. A
        mapping costs no heap and leaves the pages to the OS cache.

        Stage 4 extracts the changed .so files to work/<side>/lib/; if none are
        there, no library changed, and a byte-identical library can neither
        have gained a callback nor lost one — so the answer is no without
        reading anything.
        """
        if side not in self._native_maps:
            maps = []
            for path in sorted(
                    glob.glob(os.path.join(self.work, side, 'lib', '*', '*.so'))):
                try:
                    with open(path, 'rb') as f:
                        # mmap rejects an empty file; nothing can be found in
                        # one either, so skipping it loses nothing.
                        if os.fstat(f.fileno()).st_size:
                            maps.append(mmap.mmap(f.fileno(), 0,
                                                  access=mmap.ACCESS_READ))
                except (OSError, ValueError):
                    continue          # a directory, or a file we may not read
            self._native_maps[side] = maps
        return self._native_maps[side]


def third_party(n):
    return ["(+%d more in bundled libraries, not the app's own code)" % n] if n else []


# ---------------------------------------------------------------- detectors

def packaging(e):
    if e.mixed:
        yield Finding(0, 'MIXED PACKAGING', 'one side is an .aab, the other an .apk', [
            'res/ and resources entries are re-encoded between the two formats, so',
            'they show as changed regardless of content. dex and lib/ still compare',
            'byte-for-byte and are the parts worth reading.'])


def body_diff_unavailable(e):
    """Stage 3 could not look for rewritten bodies, so say so.

    An absent finding and a finding of absence read identically in a report,
    and only one of them is evidence. `rewritten` is the only rule that sees a
    fix which adds no signature and no string, so a run that could not compute
    it has a hole exactly where the subtlest changes live.
    """
    if e.no_body_diff:
        yield Finding(0, 'INCOMPLETE', 'method bodies were not compared', [
            'Stage 3 ran its apktool fallback, which renders bodies as text',
            'rather than bytecode, so no BEHAVIOUR CHANGE finding could be',
            'made. A method rewritten behind an unchanged signature is',
            'invisible to this run. Build the Rust helpers and re-run:',
            'cargo build --release --manifest-path rust/Cargo.toml'])


def manifest_value(e, sign, key):
    for l in e.manifest:
        if l.startswith(sign):
            m = re.search(r'\b%s=(\S+)' % re.escape(key), l)
            if m:
                return m.group(1)
    return None


def version(e):
    for key, label in (('android:versionCode', 'versionCode'),
                       ('android:versionName', 'versionName')):
        a, b = manifest_value(e, '-', key), manifest_value(e, '+', key)
        if a and b and a != b:
            yield Finding(0, 'VERSION', '%s %s -> %s' % (label, a, b), [])


def area(e):
    """The package the change is confined to, when it is confined to one.

    "every changed class is in com.example.messenger.app.calls" is the first
    thing worth knowing about a build, and the last thing the rest of this
    report says: every other rule names classes, none of them names where.
    Stage 3 prints a per-package histogram, but reading a confinement out of a
    histogram is work the reader should not have to do — and the answer is only
    interesting when there is one, so this stays silent when the change is
    spread across the app.
    """
    packages = set()
    for dex in e.dexes:
        for l in e.dex(dex, 'norm'):
            _, _, cls = l.partition('\t')
            if cls and e.is_app(cls) and '/' in cls:
                packages.add(cls.rsplit('/', 1)[0])
    if not packages:
        return
    # Segment-wise, not character-wise: com/x/audio and com/x/auth share the
    # string "com/x/au" and no package at all.
    common = []
    for seg in zip(*(p.split('/') for p in packages)):
        if len(set(seg)) != 1:
            break
        common.append(seg[0])
    prefix = '/'.join(common)
    # A prefix no deeper than the namespace every app class already shares says
    # only "the app changed", which the reader knew before opening the file.
    if not prefix or (e.ns and not prefix.startswith(e.ns + '/')):
        return
    yield Finding(0, 'CHANGE AREA',
                  'every changed class is in %s' % prefix.replace('/', '.'),
                  sorted(p.replace('/', '.') for p in packages)[:8]
                  if len(packages) > 1 else [])


def permissions(e):
    """A permission change is the one manifest edit that is always material."""
    for sign, word, rank in (('+', 'requests', 1), ('-', 'no longer requests', 1)):
        names = sorted({m.group(1) for m in (
            re.search(r'/uses-permission[^ ]*  android:name=(\S+)', l)
            for l in e.manifest if l.startswith(sign)) if m})
        if names:
            yield Finding(rank, 'PERMISSION CHANGE',
                          'the app %s %d permission(s)' % (word, len(names)),
                          names[:10])


def components(e):
    """Components declared and undeclared, and the URLs the app claims.

    These are manifest lines like any other, but 'the app registers a new
    foreground service' is a fact about the release, and reading it out of a
    raw +/- line is work the reader should not have to do.
    """
    for sign, kind, word, rank in (('+', 'NEW COMPONENT', 'declares', 1),
                                   ('-', 'REMOVED COMPONENT', 'no longer declares', 2)):
        found = [m for m in (COMPONENT.search(l) for l in e.manifest
                             if l.startswith(sign)) if m]
        if found:
            yield Finding(rank, kind, 'the app %s %d component(s)' % (word, len(found)),
                          ['%-14s %s' % (m.group(1), m.group(2)) for m in found[:12]])
    links = [m for m in (DEEPLINK.search(l) for l in e.manifest
                         if l.startswith('+')) if m]
    if links:
        yield Finding(1, 'NEW INTENT FILTER',
                      'the app claims %d new URL pattern(s)' % len(links),
                      ['%s %s' % (m.group(1), m.group(2)) for m in links[:12]])


def native_upcall(e):
    """New app methods the library names now and did not name before.

    The strongest cross-layer evidence there is, and stronger than the shared
    vocabulary `cross_layer` pairs on: `GetMethodID(cls, name, sig)` puts both
    halves in the library's .rodata as C strings, so a descriptor appearing
    there is the native side stating which Java method it intends to call. It
    is a fact about the bytes, not a guess from wording.

    The existing JNI rule cannot see this. It looks for a descriptor-shaped
    literal in the *dex*, and a method that only the native side names has none
    — `callFailed()V` was in the new library and nowhere in the Java string
    pool, so a whole cross-layer callback went unreported.

    Both sides are required: a name the old library already contained proves
    nothing, since the method may simply have been renamed on the Java side or
    the string may belong to something else entirely.
    """
    for dex in e.dexes:
        found = []
        for cls, sign, sig in _members(e, dex):
            if sign != '+' or not sig.startswith('.method') or not e.is_app(cls):
                continue
            # The descriptor is the smali declaration with its modifiers
            # removed: `callFailed()V`. Matched whole rather than by name,
            # because a bare name is a common enough word to occur in a 30MB
            # binary by accident.
            desc = sig.rsplit(' ', 1)[-1]
            if e.named_by_native(desc) and not e.named_by_native(desc, 'old'):
                found.append((cls, desc))
        if found:
            yield Finding(1, 'NEW NATIVE UPCALL',
                          '%s: the library now names %d Java method(s) it did '
                          'not name before' % (dex, len(found)),
                          ['%s.%s' % (short(c), d) for c, d in found[:8]])


def native_exports(e):
    """Symbols the library gained or lost.

    An export is the library's half of the contract with the Java side: the
    Java_* names are the JNI entry points the app calls directly, so one
    appearing is a call into native code that could not be made before.
    """
    # JNI entry points first: those are the ones the app itself calls, and the
    # rest of a Rust library's exports are its own plumbing.
    def ordered(names):
        return sorted(names, key=lambda s: (not s.startswith('Java_'), s))

    for items, kind, word, rank in (
            (ordered(e.sym_new - e.sym_old), 'NEW NATIVE EXPORT', 'exports', 2),
            (ordered(e.sym_old - e.sym_new), 'REMOVED NATIVE EXPORT',
             'no longer exports', 3)):
        if items:
            yield Finding(rank, kind, 'native: %d symbol(s) %s' % (len(items), word),
                          items[:12])


def other_entries(e):
    """Archive entries no other stage looked at.

    Everything else here reports on dex, native code and the manifest. An
    asset, a font, a bundled model file can change too, and a run that says
    nothing about them cannot be read as "nothing else changed" unless it has
    actually checked. This is that check.
    """
    for st, word, rank in (('added', 'added', 3), ('removed', 'removed', 3),
                           ('changed', 'changed', 4)):
        # Entries another stage decoded are examined, so they are not "no
        # other stage looked at" any more. The resource table joins the binary
        # XML here for exactly the reason the XML did: reported in both places
        # it is named twice, once with its content and once without.
        examined = {name for name, _ in e.axml}
        if e.res_compared:
            examined.add(arsc.ENTRY_NAME)
        rest = [n for n in e.entries[st]
                if not CODE_ENTRY.search(n) and not DERIVED_ENTRY.search(n)
                and n not in examined]
        if rest:
            yield Finding(rank, 'OTHER FILES',
                          '%d non-code file(s) %s' % (len(rest), word),
                          rest[:12])


def resource_xml(e):
    """Binary XML outside the manifest, once it has been decoded.

    These entries are compared by CRC everywhere else, which says a file moved
    and nothing about what it now says. They are declarative in the same way
    the manifest is — a FileProvider root, a backup rule, a network security
    config — so a changed line is a fact about what the app is allowed to do,
    not a rebuild artefact.
    """
    def kind(mark):
        return [(name, rows) for name, rows in e.axml
                if rows and rows[0].startswith(mark)]

    opaque = [name for name, _ in kind('!')]
    reencoded = [name for name, _ in kind('~')]
    moved = [(name, rows) for name, rows in e.axml
             if rows and not rows[0].startswith(('!', '~'))]
    if moved:
        evidence = []
        for name, rows in moved[:6]:
            evidence.append(name)
            evidence.extend('  ' + r for r in rows[:8])
            if len(rows) > 8:
                evidence.append('  (and %d more line(s))' % (len(rows) - 8))
        yield Finding(1, 'RESOURCE XML CHANGE',
                      '%d binary XML file(s) changed content' % len(moved),
                      evidence)
    if reencoded:
        # A negative result, and the reason this rule can be read as covering
        # every XML entry stage 1 flagged rather than only the interesting ones.
        yield Finding(4, 'RE-ENCODED XML',
                      '%d XML file(s) differ by CRC but not by content'
                      % len(reencoded), reencoded[:8])
    if opaque:
        yield Finding(2, 'UNDECODED XML',
                      '%d changed XML entr(y/ies) could not be decoded'
                      % len(opaque),
                      opaque[:8] + ['An .aab stores resource XML as protobuf, '
                                    'which the AXML reader cannot parse.'])


def resources(e):
    """Values out of the resource table: the app's own words, unobfuscated.

    Everything else in this report is salvage — a name R8 kept, a literal in
    the dex, a phrase recovered from .rodata. A resource entry is the one place
    where both the text a person sees and the name its author gave it survive
    the build intact, so a changed one is quotable as it stands.

    A build whose table did not change at all never reaches here: stage 2 only
    flattens the table when stage 1 says the entry differs.
    """
    if not e.res_compared:
        return
    old, new = e.res['old'], e.res['new']
    changed = sorted(k for k in old.keys() & new.keys() if old[k] != new[k])
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    if changed:
        yield Finding(1, 'REWORDED RESOURCE',
                      '%d resource value(s) changed' % len(changed),
                      ['%s\n        was  %s\n        now  %s'
                       % (k, old[k], new[k]) for k in changed[:10]])
    for keys, kind, word, rank in ((added, 'NEW RESOURCE', 'added', 1),
                                   (removed, 'REMOVED RESOURCE', 'removed', 3)):
        if keys:
            side = new if word == 'added' else old
            yield Finding(rank, kind, '%d resource value(s) %s' % (len(keys), word),
                          ['%s = %s' % (k, side[k]) for k in keys[:12]])
    was = set(e.res_meta['old'].get('languages', '').split())
    now = set(e.res_meta['new'].get('languages', '').split())
    for langs, kind, word, rank in ((sorted(now - was), 'NEW TRANSLATION',
                                     'now', 2),
                                    (sorted(was - now), 'REMOVED TRANSLATION',
                                     'no longer', 3)):
        if langs:
            yield Finding(rank, kind,
                          'the app %s carries values for %d language(s)'
                          % (word, len(langs)), [' '.join(langs)])
    if not (changed or added or removed or now - was or was - now):
        # The negative, stated rather than left as silence. The table's bytes
        # differed — that is why it was read — so "no finding" here has to be
        # distinguishable from "nobody looked", and the untranslated count is
        # what bounds the claim.
        yield Finding(4, 'RESOURCES UNCHANGED',
                      'the resource table differs but no default-configuration '
                      'value does',
                      ['%s value(s) compared; %s value(s) in %s other '
                       'configuration(s) were not'
                       % (e.res_meta['new'].get('values', '?'),
                          e.res_meta['new'].get('nondefault', '?'),
                          e.res_meta['new'].get('locales', '?'))])


def manifest_rest(e):
    other = [l for l in e.manifest
             if re.match(r'^[+-][^+-]', l)
             and not CLAIMED.search(l)
             and not COMPONENT.search(l) and not DEEPLINK.search(l)]
    if other:
        yield Finding(1, 'MANIFEST CHANGE',
                      '%d manifest line(s) changed beyond the version' % len(other),
                      other[:15])


def stub_implemented(e):
    stubs = [s for s in e.native_removed if STUB.search(s)]
    if not stubs:
        return
    # Pair each with whatever replaced it, if anything reads like a successor.
    index = TokenIndex(e.native_added, words)
    for s in stubs:
        succ = index.matches(words(s))
        yield Finding(1, 'STUB IMPLEMENTED', 'native: %r removed' % s,
                      ['replaced by: %r' % x for x in succ[:3]]
                      or ['no obvious successor string'])


def native_strings(e):
    added = [s for s in e.native_added if FAIL.search(s)]
    if added:
        yield Finding(2, 'NEW ERROR HANDLING',
                      'native: %d new failure-path log string(s)' % len(added),
                      ['%r' % s for s in added[:12]])
    # A removed failure string is the mirror image: either the condition can no
    # longer happen, or the code that reported it is gone.
    gone = [s for s in e.native_removed if FAIL.search(s) and not STUB.search(s)]
    if gone:
        yield Finding(3, 'REMOVED ERROR HANDLING',
                      'native: %d failure-path log string(s) no longer present'
                      % len(gone), ['%r' % s for s in gone[:12]])


def _members(e, dex):
    """The member rows for one dex, parsed at most once.

    Three rules want them — `members` for the signature changes, `rewritten`
    for the bodies, `hotspots` to join both per class — and the parse walks
    every member of every changed class, masking each signature on the way.
    Re-deriving it per rule made the stage cost a function of how many rules
    exist rather than of how much the build changed.
    """
    if dex not in e.member_rows:
        e.member_rows[dex] = list(_scan_members(e, dex))
    return e.member_rows[dex]


ACCESSOR = re.compile(r'\b(get|set)([A-Z]\w*)\(')
INITIALISER = re.compile(r'\.method [^(]*<c?init>\(')


def _field_name(sig):
    """The name a field is declared under, or None for a method."""
    if not sig.startswith('.field '):
        return None
    decl = sig[len('.field '):].partition(':')[0]
    return decl.rsplit(' ', 1)[-1]


def _implied(sides):
    """Members whose existence follows from another member of the same class.

    Kotlin writes a getter and a setter for every property, and the constructor
    that assigns it; a class that gained a field therefore "gains" up to three
    more members that nobody wrote and that say nothing the field has not
    already said. `InCallService` gained `_callFailed` and reported
    `getCallFailed` beside it, and the rewritten `<init>` under it, as though
    three things had happened.

    Only ever suppressed where the field it belongs to is in the same finding.
    A getter arriving alone is a property becoming readable from outside, which
    is a real change to the class's surface; a constructor rewritten on a class
    whose fields all held is a real change to what it does on construction.
    """
    fields = {}
    for sign in ('+', '-'):
        for s in sides.get(sign, ()):
            name = _field_name(s)
            if name:
                fields.setdefault(sign, set()).add(name.lstrip('_').lower())
    out = set()
    for sign, names in fields.items():
        for s in sides.get(sign, ()):
            m = ACCESSOR.search(s)
            if m and m.group(2).lower() in names:
                out.add((sign, s))
    # The constructor of a class whose fields changed necessarily changed with
    # them, and <clinit> likewise for a static one. So does everything Kotlin
    # regenerates for a data class: `ReactionTarget` gained two properties and
    # reported equals, hashCode and toString as three rewritten bodies, which
    # is the compiler restating the two fields already listed above them.
    if fields:
        for s in sides.get('~', ()):
            if INITIALISER.match(s) or DATA_CLASS.search(s):
                out.add(('~', s))
    return out


def _scan_members(e, dex):
    """(class, sign, signature) rows, with the compiler's own churn removed."""
    per = {}
    for l in e.dex(dex, 'api'):
        parts = l.split('\t', 2)
        if len(parts) == 3:
            per.setdefault(parts[0], {}).setdefault(parts[1], []).append(parts[2])
    for cls, sides in per.items():
        # An anonymous class is an implementation detail of the function that
        # created it. Kotlin renumbers the whole swarm when any of them is
        # edited, so listing their members individually buries the handful of
        # real signature changes under a hundred lambda constructors. The
        # `classes` detector reports how many appeared.
        if GENERATED.search(cls) or anonymous(cls):
            continue
        gone = {canon(s) for s in sides.get('-', ())}
        came = {canon(s) for s in sides.get('+', ())}
        implied = _implied(sides)
        for sign, opposite in (('+', gone), ('-', came)):
            for s in sides.get(sign, ()):
                if canon(s) in opposite:      # renumbered, not added or removed
                    continue
                # GENERATED has to be applied to the member too, not only the
                # class: the accessor lambdas R8 synthesises for a composable
                # (Foo$lambda$29, access$Foo$lambda$10) live on the ordinary
                # file-facade class, and they renumber whenever anything above
                # them moves.
                if PLUMBING.search(s) or SYNTHETIC.search(s) or (
                        s.startswith('.method') and GENERATED.search(s)):
                    continue
                if (sign, s) in implied:
                    continue
                yield cls, sign, s
        for s in sides.get('~', ()):
            if ('~', s) not in implied and not SYNTHETIC.search(s):
                yield cls, '~', s


def classes(e):
    """Whole classes that appeared or disappeared, named by their top level."""
    for dex in e.dexes:
        status = {}
        for l in e.dex(dex, 'norm'):
            st, _, cls = l.partition('\t')
            status.setdefault(st, []).append(cls)
        for st, kind, word, rank in (('NEW', 'NEW CLASS', 'added', 1),
                                     ('GONE', 'REMOVED CLASS', 'removed', 2)):
            # Compose emits a swarm of anonymous classes per edited function.
            # The top-level class they belong to is the finding; the swarm is
            # its size.
            named = [c for c in status.get(st, [])
                     if not anonymous(c) and not GENERATED.search(c)]
            own, others = e.split_app(named, key=lambda c: c)
            if not own and not others:
                continue
            inner = len(status.get(st, [])) - len(named)
            note = (['(plus %d anonymous/generated inner classes)' % inner]
                    if inner else [])
            yield Finding(rank, kind,
                          '%s: %d top-level class(es) %s' % (dex, len(own), word),
                          [c for c in own[:12]] + note + third_party(others))


# `<init>` and `<clinit>` open with the angle bracket, so a name class that
# does not admit one renders a constructor as `init>`.
MEMBER_NAME = re.compile(r'(<?[A-Za-z_$][\w$<>-]*)\(')


def _member_name(sig):
    """The name a method is called by, out of its smali declaration."""
    m = MEMBER_NAME.search(sig)
    return m.group(1) if m else None


def _params(sig):
    """A method's parameter types, as a person would write them."""
    open_paren, close_paren = sig.find('('), sig.rfind(')')
    if open_paren < 0 or close_paren < open_paren:
        return []
    return [type_name(d)
            for d in DESCRIPTOR.findall(sig[open_paren + 1:close_paren])]


def _modifiers(sig):
    """A member's access flags, as declared: 'private static final'."""
    body = sig.partition(' ')[2]
    return body[:body.find('(')].rsplit(' ', 1)[0] if '(' in body else ''


def _returns(sig):
    """A method's return type, as a person would write it."""
    close = sig.rfind(')')
    return type_name(sig[close + 1:]) if close >= 0 else ''


def _resignatured(e, dex):
    """Methods that kept their name and changed their parameters.

    R8 does not rename a method it is not obfuscating, so one name leaving and
    the same name arriving on the same class is a signature edit: somebody
    changed what the method takes or returns. Reported as a removal and an
    addition it is two entries fifty lines apart that never mention each other,
    and the reader has to notice the shared name to learn the one thing the
    pair actually says.

    Only a name with exactly one departure and one arrival is paired. Overloads
    genuinely coexist, and guessing which of three `AudioCall`s became which
    would be inventing a mapping the file does not state.
    """
    # Cached on the evidence for the same reason the member rows are: three
    # rules want this map, and each of them would otherwise re-walk every
    # member of every changed class to rebuild it.
    if dex not in e.pairs:
        per = {}
        for cls, sign, sig in _members(e, dex):
            if sign == '~' or not sig.startswith('.method'):
                continue
            name = _member_name(sig)
            if name:
                per.setdefault((cls, name), {}).setdefault(sign, []).append(sig)
        e.pairs[dex] = {k: (v['-'][0], v['+'][0]) for k, v in per.items()
                        if len(v.get('+', ())) == 1 and len(v.get('-', ())) == 1}
    return e.pairs[dex]


def resignatured(e):
    """One line per method whose signature moved, with what moved in it."""
    for dex in e.dexes:
        pairs = _resignatured(e, dex)
        own, others = e.split_app(sorted(pairs.items()), key=lambda i: i[0][0])
        if not own and not others:
            continue
        lines = []
        for (cls, name), (before, after) in own[:12]:
            was, now = _params(before), _params(after)
            # A multiset difference, not a set one: a method that took two
            # Function0 and now takes one has lost a parameter, and set
            # arithmetic reports that as no change at all.
            gone, came = Counter(was) - Counter(now), Counter(now) - Counter(was)
            delta = ', '.join(['+' + t for t in sorted(came.elements())]
                              + ['-' + t for t in sorted(gone.elements())])
            lines.append('%s.%s(%s)' % (short(cls), name, ', '.join(was)))
            # Equal parameter lists mean the pair differs in its return type or
            # its access flags. Printing "now (Context, String)" under an
            # identical "(Context, String)" reported a change and named none of
            # it; private becoming public is who may call the method.
            if was == now:
                lines.append('  now %s %s -> %s %s'
                             % (_modifiers(before), _returns(before),
                                _modifiers(after), _returns(after)))
            else:
                lines.append('  now (%s)%s' % (', '.join(now),
                                               '   [%s]' % delta if delta else ''))
        yield Finding(2, 'SIGNATURE CHANGE',
                      '%s: %d method(s) kept their name and changed their '
                      'declaration' % (dex, len(own)),
                      lines + third_party(others))


def members(e):
    for dex in e.dexes:
        rows = _members(e, dex)
        # Methods reported by `resignatured` as one line naming both halves.
        # Left in here as well they are two more entries, in two more lists,
        # saying less each: a report is not more complete for stating the same
        # fact three ways.
        paired = {(c, s) for (c, _), sigs in _resignatured(e, dex).items()
                  for s in sigs}
        rows = [r for r in rows if (r[0], r[2]) not in paired]
        # Cancel members that merely moved between renumbered classes: the
        # same signature under the same class name modulo digits, on both
        # sides, is the compiler reshuffling rather than an edit.
        def key(c, sig):
            return canon(c) + '\x00' + canon(sig)

        seen = {'+': {key(c, s) for c, g, s in rows if g == '+'},
                '-': {key(c, s) for c, g, s in rows if g == '-'}}
        rows = [r for r in rows
                if r[1] == '~' or key(r[0], r[2]) not in seen['-' if r[1] == '+' else '+']]

        for sign, kinds in (('+', (('.method', 'NEW API SURFACE', 'method'),
                                   ('.field', 'NEW STATE', 'field'))),
                            ('-', (('.method', 'REMOVED API', 'method'),
                                   ('.field', 'REMOVED STATE', 'field')))):
            for prefix, kind, word in kinds:
                items = [(c, s) for c, g, s in rows
                         if g == sign and s.startswith(prefix)]
                own, others = e.split_app(items)
                if not own and not others:
                    continue
                yield Finding(2 if sign == '+' else 3, kind,
                              '%s: %d %s %s(s) in %s'
                              % (dex, len(own), 'new' if sign == '+' else 'removed',
                                 word, e.ns or 'the build'),
                              [pretty(s, c) for c, s in own[:15]]
                              + third_party(others))


def rewritten(e):
    """Members whose signature held but whose body was rewritten.

    A fix that only rewires control flow adds no signature and no string, so
    every other rule is blind to it. What it does change is the opcode sequence,
    which the dex reader records.
    """
    for dex in e.dexes:
        items = [(c, s) for c, g, s in _members(e, dex) if g == '~']
        own, others = e.split_app(items)
        if not own:
            continue
        by_class = {}
        for c, s in own:
            by_class.setdefault(c, []).append(pretty(s, c))
        lines = []
        for c in sorted(by_class, key=lambda c: (-len(by_class[c]), c))[:12]:
            for s in sorted(by_class[c])[:3]:
                lines.append(s)
        yield Finding(3, 'BEHAVIOUR CHANGE',
                      '%s: %d method(s) rewritten on %d class(es), same signature'
                      % (dex, len(own), len(by_class)),
                      lines + third_party(others))


def _owners(e, dex):
    """literal -> the app classes whose code loads it, by top-level name."""
    out = {}
    for l in e.dex(dex, 'owner'):
        lit, _, cls = l.rpartition('\t')
        if e.is_app(cls):
            out.setdefault(lit, set()).add(outer(cls))
    return out


def strings(e):
    for dex in e.dexes:
        old = set(e.dex(dex, 'str.old'))
        new = set(e.dex(dex, 'str.new'))
        owners = _owners(e, dex)

        def clean(items):
            return sorted(s for s in items
                          if not NOISE_STR.search(s) and not GENERATED.search(s))

        gained, lost = clean(new - old), clean(old - new)

        def attributed(items):
            out = []
            for s in items:
                who = sorted(owners.get(s, ()))
                out.append('%s%s' % (s, '   [%s]' % ', '.join(who[:2]) if who else ''))
            return out

        def user_facing(items):
            """Text a person reads: a capitalised phrase, no descriptors."""
            return [s for s in items
                    if len(s) > 14 and ' ' in s.strip('"')
                    and re.match(r'^"[A-Z]', s)
                    and not re.search(r'[/;]|com\.|androidx', s)]

        ui, ui_gone = user_facing(gained), user_facing(lost)
        # A reworded message is one string leaving and its near-twin arriving.
        # Left unpaired those are two findings that do not mention each other,
        # and the removal is not reported at all — so "Failed to get live view"
        # becoming "Failed to get live view for guid=" read as a brand new
        # message and a silent deletion.
        index = TokenIndex(ui_gone, concepts)
        reworded, spent = [], set()
        for s in ui:
            hit = [h for h in index.matches(concepts(s)) if h not in spent]
            if hit:
                spent.add(hit[0])
                reworded.append((hit[0], s))
        # Both memberships hoisted into sets built once. A set display written
        # inside a comprehension is rebuilt on every item, and `x not in list`
        # is a scan; neither shows on today's inputs, where a build has a
        # handful of user-facing strings, but both are quadratic in a quantity
        # nothing here bounds.
        already_ui = set(ui)
        paired = {n for _, n in reworded}
        fp = [s for s in gained
              if FAIL.search(s) and s not in already_ui and len(s) > 10]
        ui = [s for s in ui if s not in paired]
        ui_gone = [s for s in ui_gone if s not in spent]
        jni = [s for s in gained
               if JNI.match(s.strip('"')) and e.called_from_native(s.strip('"'))]
        gone_fp = [s for s in lost if FAIL.search(s) and len(s) > 10]

        if ui:
            yield Finding(2, 'NEW USER-FACING TEXT',
                          '%s: %d new message(s) shown to the user' % (dex, len(ui)),
                          attributed(ui[:10]))
        if reworded:
            yield Finding(2, 'REWORDED TEXT',
                          '%s: %d message(s) shown to the user were reworded'
                          % (dex, len(reworded)),
                          ['was %s\n        now %s' % (a, b)
                           for a, b in reworded[:8]])
        if ui_gone:
            yield Finding(3, 'REMOVED USER-FACING TEXT',
                          '%s: %d message(s) no longer shown to the user'
                          % (dex, len(ui_gone)), ui_gone[:10])
        if fp:
            yield Finding(3, 'NEW ERROR HANDLING',
                          '%s: %d new failure-path string(s)' % (dex, len(fp)),
                          attributed(fp[:12]))
        if gone_fp:
            yield Finding(4, 'REMOVED ERROR HANDLING',
                          '%s: %d failure-path string(s) no longer present'
                          % (dex, len(gone_fp)), gone_fp[:12])
        if jni:
            yield Finding(2, 'NEW NATIVE CALLBACK',
                          '%s: JNI descriptor(s) appeared' % dex, jni[:6])


# Evidence kinds a class can carry, in the order they read best under one
# heading: what it gained, what it lost, what it rewrote.
CLASS_EVIDENCE = (('+', '.field', 'new field', 'new fields'),
                  ('+', '.method', 'new method', 'new methods'),
                  ('-', '.field', 'removed field', 'removed fields'),
                  ('-', '.method', 'removed method', 'removed methods'),
                  ('~', None, 'body rewritten', 'bodies rewritten'))


def hotspots(e):
    """One class's whole story under one heading.

    Every other dex rule is organised by kind of evidence: new methods in one
    finding, new fields in another, rewritten bodies in a third, the messages
    they load in a fourth. That is the right shape for asking "what kind of
    change was this" and the wrong one for asking "what happened to
    InCallService" — whose answer is otherwise spread across four
    sections, in four different orders, for the reader to join by eye. A
    release note is written the second way round, so the report should be able
    to answer that question without being re-sorted by hand.

    Only classes carrying more than one kind of evidence appear. A class with
    nothing but three new methods is already legible in NEW API SURFACE, and
    repeating it here would be length without information; a class that gained
    a field, gained the two methods that expose it, had a constructor rewritten
    and now loads a new message is a change nobody can see whole anywhere else.
    """
    for dex in e.dexes:
        # Group by top-level class, the same unit `outer` gives the string
        # owners, so members and the text they load land under one name.
        per = {}
        for cls, sign, sig in _members(e, dex):
            if not e.is_app(cls):
                continue
            kind = '~' if sign == '~' else sig.split(' ', 1)[0]
            per.setdefault(outer(cls), {}).setdefault((sign, kind), []).append((cls, sig))

        owners = _owners(e, dex)
        gained = set(e.dex(dex, 'str.new')) - set(e.dex(dex, 'str.old'))
        loads = {}
        for lit in sorted(gained):
            if NOISE_STR.search(lit) or GENERATED.search(lit):
                continue
            for who in owners.get(lit, ()):
                loads.setdefault(who, []).append(lit)

        def weight(name):
            members = per.get(name, {})
            return (sum(len(v) for v in members.values()) + len(loads.get(name, ())),
                    name)

        ranked = sorted(set(per) | set(loads), key=weight, reverse=True)
        for name in ranked:
            members, text = per.get(name, {}), loads.get(name, [])
            kinds = [(sign, k, one, many) for sign, k, one, many in CLASS_EVIDENCE
                     if members.get((sign, k) if sign != '~' else ('~', '~'))]
            # More than one kind of evidence is what makes the join worth
            # printing; one kind is already reported, better, by its own rule.
            if len(kinds) + bool(text) < 2:
                continue
            counts, lines = [], []
            for sign, k, one, many in kinds:
                rows = members[(sign, k) if sign != '~' else ('~', '~')]
                counts.append('%d %s' % (len(rows), one if len(rows) == 1 else many))
                for cls, sig in sorted(rows, key=lambda r: r[1])[:4]:
                    lines.append('%s %s' % (sign, pretty(sig)))
                if len(rows) > 4:
                    lines.append('%s (and %d more)' % (sign, len(rows) - 4))
            if text:
                counts.append('%d new string%s'
                              % (len(text), '' if len(text) == 1 else 's'))
                lines.extend('  loads %s' % t for t in text[:4])
                if len(text) > 4:
                    lines.append('  loads (and %d more)' % (len(text) - 4))
            yield Finding(1, 'CHANGED CLASS', '%s: %s' % (name, ', '.join(counts)),
                          lines)



def cross_layer(e):
    """The same concept newly named in both halves.

    A fix that spans Rust and Kotlin is the strongest signal there is, because
    neither side alone proves intent but both sides agreeing does.
    """
    dexnew = set()
    for dex in e.dexes:
        dexnew |= set(e.dex(dex, 'str.new')) - set(e.dex(dex, 'str.old'))
    # Sorted, because only the first match is reported: iterating the set
    # directly made which java string got named depend on hash order, so two
    # runs over the same pair of builds could disagree.
    index = TokenIndex(sorted(dexnew), concepts)
    seen, pairs = set(), []
    for a in e.native_added:
        ta = concepts(a)
        if len(ta) < 2 or a in seen:
            continue
        hit = index.matches(ta)
        if hit:
            seen.add(a)
            pairs.append((a, hit[0]))
    if pairs:
        yield Finding(1, 'CROSS-LAYER CHANGE',
                      '%d concept(s) added on BOTH the native and Java side'
                      % len(pairs),
                      ['native %r\n        java   %s' % (a, b) for a, b in pairs[:8]])


DETECTORS = (packaging, body_diff_unavailable,
             version, area, permissions, components, resource_xml, resources,
             manifest_rest,
             stub_implemented, native_strings, native_upcall,
             native_exports, classes,
             resignatured, members,
             rewritten, strings, cross_layer, hotspots, other_entries)


# ---------------------------------------------------------------- output

def headline(e):
    """Who this build is and how much of it moved.

    A findings list on its own does not say which app, which version, or how
    much of the archive it accounts for — all of which the reader otherwise
    goes digging through three other reports for.
    """
    who = ' '.join(x for x in (e.about.get('package'),
                               e.about.get('versionName'),
                               '(%s)' % e.about['versionCode']
                               if e.about.get('versionCode') else '') if x)
    moved = sum(len(v) for v in e.entries.values())
    scale = ''
    if moved:
        of = ' of %d' % e.total_entries if e.total_entries else ''
        scale = ('%d%s archive entry differs' % (moved, of) if moved == 1
                 else '%d%s archive entries differ' % (moved, of))
    return [l for l in (who, scale) if l]


def render(findings, e=None):
    head = headline(e) if e else []
    if head:
        head.append('')
    if not findings:
        return '\n'.join(head + ['No heuristic findings. '
                                  'Read the per-stage reports directly.', ''])
    out = list(head)
    for f in sorted(findings, key=lambda f: f.rank):
        out.append('[%s] %s' % (f.kind, f.title))
        out.extend('        %s' % e for e in f.evidence)
        out.append('')
    return '\n'.join(out) + '\n'


def main(argv):
    if len(argv) != 3:
        sys.stderr.write('usage: findings.py WORKDIR REPORTDIR\n')
        return 2
    e = Evidence(argv[1], argv[2])
    text = render([f for d in DETECTORS for f in d(e)], e)
    # UTF-8 explicitly, on both the file and stdout. The whole point of this
    # stage is to quote the app's own text, and an app's text is not ASCII: a
    # curly quote in a user-facing string is enough to abort the run under a
    # locale that leaves Python's default encoding at ASCII. `errors` is set on
    # stdout only, where the destination may genuinely be unable to carry a
    # character; the report file has no such excuse.
    with open(os.path.join(argv[2], '7-findings.txt'), 'w',
              encoding='utf-8') as f:
        f.write(text)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
