"""Stage 8: assemble the evidence into a release-note draft.

    notes.py WORKDIR REPORTDIR [--into NOTESFILE]

Stage 7 answers "what did this build change", filed by kind of evidence: new
methods in one section, rewritten bodies in another, the strings they load in a
third. A release note is filed the other way round — by the behaviour the
change adds up to, with the evidence underneath it — and that reordering, plus
the skeleton around it, plus the transcription of every name and literal, is
most of the work of writing one and none of the judgement.

So this writes the draft, and states what it cannot do.

**What it decides.** Which findings belong to the same change (they touch one
package, or they share vocabulary); which section that change belongs in; which
sentence leads it; how the archive-level scope reads. Every one of those is a
rule you can read below and disagree with.

**What it does not decide.** Why anything changed, what the user experienced
before it, or whether a finding is worth reporting at all. The lead sentences
here are descriptive by construction — "`FaceTimeInCallService` gained
`callFailed()`, and the native library gained a log string naming the same
thing" — where a finished note leads with "a call whose media link died left
the app hung, with no way out". Turning the first into the second is reading
the evidence and knowing the app, and nothing here can do it.

So the draft is a scaffold with the facts already in the right places and the
sentences still to write. Its last section is a ledger of every stage-7 finding
and where it went, so that editing it down stays a decision rather than an
oversight.
"""
import os
import re
import sys
from collections import Counter, defaultdict, namedtuple

sys.path.insert(0, __file__.rsplit('/', 1)[0])
import findings                                              # noqa: E402
from findings import (DETECTORS, Evidence, GENERATED, NOISE_STR,  # noqa: E402
                      FAIL, STUB, concepts, outer, pretty, short)

BEGIN, END = '<!-- apkdiff:draft-begin -->', '<!-- apkdiff:draft-end -->'

# One method that kept its name. Three things can have moved — what it takes,
# what it returns, and who may call it — and a tuple of positions made every
# use of it a guess about which field was which.
SigChange = namedtuple('SigChange', 'name was now ret_was ret_now mods_was mods_now')

# Findings that describe the run rather than the build. They are facts about
# how much this comparison could see, which belongs in the draft — as a caveat
# the writer has to resolve — but never as a change the release made.
CAVEAT = frozenset(('MIXED PACKAGING', 'INCOMPLETE', 'UNDECODED XML'))
# Findings whose whole content is that something did NOT change. The notes
# guide asks for these by name: a negative result with its numbers is worth
# writing down, and it is the only thing that stops a reader assuming nobody
# looked.
NEGATIVE = frozenset(('RE-ENCODED XML', 'RESOURCES UNCHANGED'))
# Findings a topic already carries, class by class and string by string. They
# are the same facts re-filed by kind, which is what stage 7 is for and what a
# note is not.
ABSORBED = frozenset((
    'CHANGED CLASS', 'NEW CLASS', 'REMOVED CLASS', 'NEW API SURFACE',
    'REMOVED API', 'NEW STATE', 'REMOVED STATE', 'SIGNATURE CHANGE',
    'BEHAVIOUR CHANGE', 'NEW USER-FACING TEXT', 'NEW ERROR HANDLING',
    'REMOVED ERROR HANDLING', 'STUB IMPLEMENTED', 'CROSS-LAYER CHANGE',
    'CHANGE AREA', 'VERSION', 'NEW NATIVE UPCALL'))
# `attach_upcalls` puts each one under the class it names; see there for why
# that cannot fail.
UPCALL = 'NEW NATIVE UPCALL'
# `was "…"` / `now "…"`, the shape `strings` writes a rewording in.
REWORD = re.compile(r'^was (.*)\n\s+now (.*)$', re.S)
# Everything else becomes an entry of its own. Note which kinds are NOT in the
# set above: a dex literal that disappeared has no owner — stage 3's owner
# artefact maps the literals a build gained — so no class can be named for it,
# and claiming it was absorbed into a class entry would make the ledger's
# guarantee a false one. The same goes for a JNI descriptor, which is evidence
# about the boundary rather than about either side of it.

# A message a person reads, as opposed to one a log file does: a capitalised
# phrase with a space in it and no descriptor or package path. Same test the
# `strings` detector applies, kept in one place so the draft quotes exactly the
# strings stage 7 called user-facing.
def user_facing(s):
    return (len(s) > 14 and ' ' in s.strip('"') and re.match(r'^"[A-Z]', s)
            and not re.search(r'[/;]|com\.|androidx', s))


CAMEL = re.compile(r'[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+')


def name_words(name):
    """The words inside an identifier: FaceTimeInCallService -> face time..."""
    return {w.lower() for w in CAMEL.findall(name) if len(w) > 3}


def quote(lit, bold=True):
    """A dex literal as the note should print it.

    User-facing copy gets curly quotes and bold, which is the notes file's
    convention for "this is the app's own words, shown to somebody"; anything
    else is code and gets a backtick. Inside a lead sentence the bold is
    dropped, because the whole sentence is already bold and markdown renders
    the nested pair as literal asterisks.
    """
    body = lit.strip('"')
    if user_facing(lit):
        return '**“%s”**' % body if bold else '“%s”' % body
    return '`%s`' % body


def plural(n, one, many=None):
    """"1 method" / "2 methods".

    Worth a function because a draft is read as prose: "1 new method(s)" in
    every second bullet is the sort of thing that makes a generated document
    look generated, and this file is meant to be edited into a real note
    rather than rewritten from scratch.
    """
    return '%d %s' % (n, one if n == 1 else (many or one + 's'))


def member_label(sig):
    """A member as it should read inside a sentence.

    `pretty` renders a declaration — modifiers, owning class, parameters —
    which is right for a report that lists one per line and wrong inside a
    comma-separated run, where "private static final FaceTimeCallScreenKt.
    AudioCall" is forty characters carrying one word of information. The class
    is already the bullet's subject and the modifiers are in stage 3's report.
    """
    head = sig.split('(')[0]
    field = head.split(':')[0] if ':' in sig and '(' not in sig else head
    name = field.rsplit(' ', 1)[-1].rsplit('.', 1)[-1]
    kind = sig.partition(':')[2].strip() if '(' not in sig and ':' in sig else ''
    return '%s: %s' % (name, kind) if kind else name


def bullets(items, cap, more='and %d more'):
    """A capped list, with what was left out stated rather than dropped."""
    out = list(items[:cap])
    if len(items) > cap:
        out.append('(%s)' % (more % (len(items) - cap)))
    return out


class Topic:
    """One change, as the note will tell it.

    A topic is the join stage 7 deliberately does not make: the classes that
    moved together, the strings they load, and the native evidence that names
    the same thing. It exists because a reader asks "what happened to the
    FaceTime call screen", and the answer is otherwise spread across six
    findings in four orders.
    """

    def __init__(self, key, title):
        self.key = key
        self.title = title
        self.classes = {}          # short name -> dict of evidence lists
        self.native = []           # (kind, string) pairs
        self.upcalls = []          # (class, descriptor) the library now names
        self.rewords = []          # (class, before, after) for its own text
        self.native_gone = []      # strings the library no longer contains
        self.stubs = []            # (removed stub string, successors)
        self.tokens = set()
        self.extra = []            # rendered lines from non-dex findings

    # --- what kind of change this is, from what it carries -----------------

    @property
    def user_text(self):
        return [s for c in self.classes.values() for s in c['loads']
                if user_facing(s)]

    @property
    def fail_text(self):
        return ([s for c in self.classes.values() for s in c['loads']
                 if FAIL.search(s) and not user_facing(s)]
                + [s for kind, s in self.native if kind == 'fail'])

    @property
    def new_types(self):
        """The types this change introduced, nested ones included."""
        return sorted(name for ev in self.classes.values()
                      for st, name in ev['status'] if st == 'NEW')

    @property
    def lead_class(self):
        """The class the entry is about: the one carrying the most evidence."""
        if not self.classes:
            return None
        return max(sorted(self.classes),
                   key=lambda c: _class_weight(self.classes[c]))

    @property
    def subject(self):
        """What the lead sentence calls this topic.

        A topic assembled only from native evidence has no class to name — the
        library is stripped, so there is nothing to name it with — and every
        sentence that would have put a class in backticks has to say so rather
        than print an empty pair of them.
        """
        cls = self.lead_class
        return '`%s`' % cls if cls else 'the native library' 

    def counts(self):
        n = Counter()
        for ev in self.classes.values():
            for k in ('+method', '-method', '+field', '-field', '~', 'sig'):
                n[k] += len(ev[k])
        n['native'] = len(self.native)
        n['stub'] = len(self.stubs)
        return n

    def section(self):
        """New, Changed or Fixed — and why, in one place.

        These three rules are the draft's biggest guess. They are stated
        positively and in order so that a reader who disagrees can see which
        one fired: a stub that is now implemented, or a failure path that
        reports something it did not report before, is a fix; a type or a
        capability that did not exist is new; everything else is a change to
        something that already worked.
        """
        n = self.counts()
        api = n['+method'] + n['+field']
        # A failure path that reports something new is a fix only where there
        # is not a feature around it. Three new methods and a new field beside
        # one "could not" string is somebody building something that handles
        # its own errors, and filing that under Fixed loses the feature and
        # invents a bug — which the ProfileStore offer work did on the first
        # draft of this rule.
        if self.stubs or (self.fail_text and not self.new_types and api <= 2):
            return 'Fixed'
        if self.new_types or (n['+method'] and self.user_text
                              and not self.fail_text):
            return 'New'
        return 'Changed'

    def weight(self):
        n = self.counts()
        return (sum(n.values()) + len(self.user_text), self.title)


# ---------------------------------------------------------------- gathering

def collect(e):
    """Every app class that moved, with everything the run knows about it.

    One pass over the artefacts producing one record per class, rather than
    each rule re-deriving what it needs: the members, the signatures that were
    reworked, the literals the class loads, whether the class itself is new,
    and which package it lives in.
    """
    per = defaultdict(lambda: defaultdict(list))
    new_types = set()
    for dex in e.dexes:
        packages = {}
        for l in e.dex(dex, 'norm'):
            st, _, cls = l.partition('\t')
            packages[cls] = st
        owners = findings._owners(e, dex)
        gained = set(e.dex(dex, 'str.new')) - set(e.dex(dex, 'str.old'))

        pairs = findings._resignatured(e, dex)
        paired = {(c, sig) for (c, _), sigs in pairs.items() for sig in sigs}
        for cls, sign, sig in findings._members(e, dex):
            if not e.is_app(cls) or (cls, sig) in paired:
                continue
            kind = '~' if sign == '~' else sign + sig.split(' ', 1)[0].lstrip('.')
            slot = per[outer(cls)]
            slot[kind].append(pretty(sig, cls))
            slot['types'].extend(findings._params(sig))
        for (cls, name), (before, after) in sorted(pairs.items()):
            if not e.is_app(cls):
                continue
            slot = per[outer(cls)]
            slot['sig'].append(SigChange(
                name, findings._params(before), findings._params(after),
                findings._returns(before), findings._returns(after),
                findings._modifiers(before), findings._modifiers(after)))
            slot['types'].extend(findings._params(after))
        for lit in sorted(gained):
            if NOISE_STR.search(lit) or GENERATED.search(lit):
                continue
            for who in owners.get(lit, ()):
                per[who]['loads'].append(lit)
        # A class that is wholly new or gone is the strongest single fact about
        # it, and it lives in a different artefact from its members. A nested
        # one is recorded against the class that contains it — StoredOffer is
        # ProfileStore's news, not a topic of its own — but keeps its own name,
        # because "a new nested type" and "a new top-level class" are different
        # facts and the reader can tell them apart from the name alone.
        for cls, st in packages.items():
            if not e.is_app(cls) or st not in ('NEW', 'GONE'):
                continue
            if findings.anonymous(cls) or GENERATED.search(cls):
                continue
            per[outer(cls)]['status'].append((st, short(cls)))
            if st == 'NEW':
                new_types.add(short(cls).rsplit('$', 1)[-1])
        for cls in list(per):
            for full, _ in packages.items():
                if outer(full) == cls:
                    per[cls]['package'] = [full.rsplit('/', 1)[0].replace('/', '.')]
                    break
    for cls, ev in per.items():
        ev.setdefault('package', [e.ns.replace('/', '.') if e.ns
                                  else '(unknown package)'])
    return per, new_types


# Types so common in a Compose build that two classes sharing one are not
# related by it: the runtime's own vocabulary, the Kotlin function types, and
# the primitives. Linking on these would join every composable in the app.
# Up to this many changed classes, a package is reported as one change. Set
# from the two builds this was written against: the FaceTime fix touches three
# classes and is one note, the conversation screen touches fifteen and is four.
WHOLE_PACKAGE = 4

UBIQUITOUS = frozenset((
    'Composer', 'Modifier', 'int', 'boolean', 'long', 'float', 'double',
    'String', 'List', 'Set', 'Map', 'Object', 'void', 'byte', 'char', 'short',
    'Continuation', 'Bundle', 'Context', 'View'))


def topics_from(per, new_types):
    """Split the changed classes into the changes a note would describe.

    The package is the outer bound — two classes in different packages are two
    entries, which is the same judgement `CHANGE AREA` makes — but a package
    like `app.messaging` holds a whole screen, and a build that touches four
    unrelated things in it is four notes, not one.

    So within a package, two classes are joined when the evidence itself says
    they moved together:

    * they both name a type that is **new in this build**. That is what
      threading a new value through a hierarchy looks like from the outside,
      and it is precise because the type did not exist to be named before.
    * their new string literals share at least two content words — the same
      threshold every other pairing in this tool uses.

    Nothing joins on a type that already existed: in a Compose build every
    composable takes a `Composer`, and linking on that produces one topic
    containing the app.
    """
    parent = {c: c for c in per}

    def find(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    same_package = defaultdict(list)
    for cls, ev in per.items():
        same_package[ev['package'][0]].append(cls)
    for members in same_package.values():
        by_type = defaultdict(list)
        by_token = defaultdict(list)
        for cls in members:
            for t in set(per[cls]['types']) & new_types - UBIQUITOUS:
                by_type[t].append(cls)
            # The class that declares the new type belongs with the classes
            # that now take it. It does not name itself in its own signatures,
            # so nothing else here would join them: the gestures type came out
            # as an entry saying only "this is new", while the ten signatures
            # it had been threaded through sat under a different heading.
            for st, name in per[cls]['status']:
                if st == 'NEW' and name.rsplit('$', 1)[-1] in new_types:
                    by_type[name.rsplit('$', 1)[-1]].append(cls)
            for lit in per[cls]['loads']:
                for tok in concepts(lit):
                    by_token[tok].append(cls)
        for group in by_type.values():
            for other in group[1:]:
                union(group[0], other)
        # Two shared tokens, counted per pair: one shared word is met by any
        # two messages from the same screen.
        shared = Counter()
        for group in by_token.values():
            for i, a in enumerate(sorted(set(group))):
                for b in sorted(set(group))[i + 1:]:
                    shared[(a, b)] += 1
        for (a, b), n in shared.items():
            if n >= 2:
                union(a, b)
        # A package with a handful of changed classes is one change, whatever
        # the components say. Three classes that share only the word "failed"
        # are the same fix seen from three layers — splitting them produced
        # three entries, each too small to lead a sentence, where the build had
        # made exactly one change. Above that size a package is a screen or a
        # subsystem, several things happen in it at once, and the components
        # are the only evidence available for which of them go together.
        if len(members) <= WHOLE_PACKAGE:
            for other in members[1:]:
                union(members[0], other)

    topics = {}
    for cls in sorted(per):
        root = find(cls)
        pkg = per[cls]['package'][0]
        topic = topics.setdefault(root, Topic(root, pkg))
        topic.classes[cls] = per[cls]
        topic.tokens |= name_words(cls)
        for lit in per[cls]['loads']:
            topic.tokens |= concepts(lit)
    return topics


def dex_topics(e):
    per, new_types = collect(e)
    return topics_from(per, new_types)


def attach_native(e, topics):
    """Native evidence, filed under the topic that names the same thing.

    A native string and a dex class belong together when they share at least
    two content words — the same test `cross_layer` uses, and for the same
    reason: one side alone proves nothing, both sides naming a concept is the
    strongest signal in the report. What matches nothing gets its own topic
    rather than being dropped, because an unattached native change is still a
    change and the writer has to see it.
    """
    loose = Topic('(native)', 'the native library')
    # Which topic loads which new dex literal. A native string that shares
    # vocabulary with a literal belongs with the class that loads it, not
    # merely with a package that mentions the same words — this is the pairing
    # `cross_layer` reports, used here to file rather than to report.
    by_literal = {}
    for t in topics.values():
        for ev in t.classes.values():
            for lit in ev['loads']:
                by_literal.setdefault(lit, t)
    index = findings.TokenIndex(sorted(by_literal), concepts)
    only = list(topics.values())[0] if len(topics) == 1 else None

    def home(tokens):
        """Where a native string belongs, strongest evidence first.

        A literal on the other side of the JNI boundary naming the same thing
        is the best link there is; shared vocabulary with a whole topic is the
        next best. Two shared content words is the threshold `cross_layer`
        uses — one is met by any two sentences about the same subsystem, and
        would file every native string under whichever topic was largest.

        Failing both, a build with exactly one changed package has only one
        place a native change can belong: the library and that package are the
        whole of what moved.
        """
        hit = index.matches(tokens)
        if hit:
            return by_literal[hit[0]]
        best, score = None, 1
        for t in topics.values():
            n = len(tokens & t.tokens)
            if n > score:
                best, score = t, n
        return best or only or loose

    for s in e.native_added:
        home(concepts(s)).native.append(('fail' if FAIL.search(s) else 'plain', s))
    succ_index = findings.TokenIndex(e.native_added, findings.words)
    for s in e.native_removed:
        if not STUB.search(s):
            # Not a stub, so nothing implemented it: the condition it reported
            # either cannot arise any more or is no longer reported. Either way
            # it is evidence, and dropping it here while the ledger below said
            # it had been absorbed would make that claim untrue.
            home(concepts(s)).native_gone.append(s)
            continue
        # A stub string is short and mostly stop-words ("TODO it didn't work"),
        # so it rarely shares two content words with anything. What does place
        # it is whatever replaced it, which is a real sentence.
        succ = succ_index.matches(findings.words(s))[:3]
        home(concepts(' '.join(succ)) or concepts(s)).stubs.append((s, succ))
    # native_gone as well as the two above: a build whose library only lost
    # strings, and whose dex gives them nowhere to belong, put every removal on
    # this topic and then discarded it — while the ledger went on calling
    # REMOVED ERROR HANDLING absorbed, and the scope line called the build
    # packaging-only. Retaining a topic has to test everything a topic can
    # carry, not the two kinds that happened to be listed first.
    if loose.native or loose.stubs or loose.native_gone:
        topics[loose.key] = loose


# ---------------------------------------------------------------- rendering

def lead(topic):
    """The bold sentence a topic opens with.

    Ordered by how much the evidence pins down, strongest first. Every one of
    these is a statement about what the build contains, never about why: the
    sentence a finished note leads with — what broke, and what the user saw —
    is not in the file and cannot be derived from it.
    """
    n = topic.counts()
    if topic.stubs:
        return ('A native code path that was a stub is now implemented: %s is '
                'gone from the library.'
                % ', '.join('`%s`' % s for s, _ in topic.stubs[:2]))
    new = topic.new_types
    if new:
        # A new type named in signatures that already existed is a value being
        # threaded through code that did not carry it before — the most
        # informative shape a new class has, and the reason the count is here
        # rather than only in the bullets below.
        threaded = sum(1 for c in topic.classes.values() for row in c['sig']
                       if any(t in new for t in row.now))
        if threaded:
            return ('`%s` is a new type, and it now appears in %s.'
                    % (new[0], plural(threaded, 'changed method signature')))
        return ('%s %s new.' % (', '.join('`%s`' % c for c in new[:3]),
                                'is' if len(new) == 1 else 'are'))
    if topic.user_text:
        who = sorted(c for c, ev in topic.classes.items()
                     if any(user_facing(s) for s in ev['loads']))
        return ('The app shows %s it did not have before — %s, loaded by %s.'
                % (plural(len(topic.user_text), 'message'),
                   ', '.join(quote(s, bold=False)
                             for s in sorted(topic.user_text)[:2]),
                   ', '.join('`%s`' % c for c in who[:3])))
    if topic.fail_text:
        n_fail = len(topic.fail_text)
        return ('%s in %s now %s a condition that went unreported before: '
                '%s.'
                % (plural(n_fail, 'failure path'), topic.subject,
                   'reports' if n_fail == 1 else 'report',
                   ', '.join(quote(s, bold=False)
                             for s in sorted(topic.fail_text)[:2])))
    if n['sig']:
        named = [row.name for ev in topic.classes.values() for row in ev['sig']]
        return ('%s in %s kept %s name and changed %s: %s.'
                % (plural(n['sig'], 'method'), topic.subject,
                   'its' if n['sig'] == 1 else 'their',
                   'what it takes' if n['sig'] == 1 else 'what they take',
                   ', '.join('`%s`' % x for x in sorted(named)[:3])))
    if n['+method'] or n['+field']:
        # The class that gained something, not the one with the most evidence:
        # a topic where one class was rewritten and another gained a method led
        # with "`Chat` gained 1 method" over a bullet showing the method on
        # `Deletion`.
        who = max(sorted(topic.classes),
                  key=lambda c: len(topic.classes[c]['+method'])
                  + len(topic.classes[c]['+field']))
        ev = topic.classes[who]
        gains = [plural(len(ev[k]), w) for k, w in (('+method', 'method'),
                                                    ('+field', 'field')) if ev[k]]
        return ('`%s` gained %s.' % (who, ' and '.join(gains)))
    if n['~'] == 1:
        only = [member_label(s) for ev in topic.classes.values() for s in ev['~']]
        return ('`%s.%s` was rewritten; its signature did not change.'
                % (topic.lead_class, only[0]))
    if n['-method'] or n['-field']:
        losses = [plural(n[k], w) for k, w in (('-method', 'method'),
                                               ('-field', 'field')) if n[k]]
        return ('%s no longer has %s.' % (topic.subject.capitalize()
                                          if not topic.lead_class
                                          else topic.subject,
                                          ' or '.join(losses)))
    if n['~']:
        return ('%s rewritten in %s without any signature changing.'
                % (plural(n['~'], 'method was', 'methods were'), topic.subject))
    if not topic.native:
        # Removals and nothing else. The sentence stops at what the library no
        # longer contains: whether the condition can no longer arise or is
        # merely no longer reported is exactly the distinction a stripped
        # binary cannot settle, and either would be a claim about why.
        return ('The native library no longer contains %s.'
                % plural(len(topic.native_gone), 'log string'))
    # Only native evidence, and none of it a stub or a failure path: a library
    # that gained log strings saying nothing this file can classify. Reported
    # as what it is rather than forced into one of the shapes above.
    return ('The native library gained %s and nothing in the dex changed with '
            'them.' % plural(len(topic.native), 'log string'))


def sig_line(row):
    """One reworked signature, with what moved in it called out.

    The parameter lists alone make the reader diff two forty-character strings
    by eye. The bracket at the end is that diff, as a multiset difference —
    a method that took two `Function0` and now takes one has lost a parameter,
    and set arithmetic reports that as no change at all.
    """
    gone = Counter(row.was) - Counter(row.now)
    came = Counter(row.now) - Counter(row.was)
    delta = ', '.join(['+`%s`' % t for t in sorted(came.elements())]
                      + ['-`%s`' % t for t in sorted(gone.elements())])
    returns = ('' if row.ret_was == row.ret_now
               else ', and returns `%s` rather than `%s`'
               % (row.ret_now, row.ret_was))
    if row.was != row.now:
        return ('`%s(%s)` now takes `(%s)`%s%s'
                % (row.name, ', '.join(row.was), ', '.join(row.now),
                   ' — %s' % delta if delta else '', returns))
    # Same name, same parameters. Printing the two identical lists side by side
    # said "now takes (Context, String)" of a method that already took exactly
    # that; what actually moved is the return type or the access flags, and a
    # method going from private to public is a fact about who can call it.
    if returns:
        return ('`%s(%s)` now returns `%s` rather than `%s`'
                % (row.name, ', '.join(row.now), row.ret_now, row.ret_was))
    return ('`%s(%s)` is declared `%s`, and was `%s`'
            % (row.name, ', '.join(row.now), row.mods_now, row.mods_was))


def class_rows(ev):
    """What one class did, one line per kind of evidence.

    Kinds in a fixed order — what the class became, what its callers now have
    to pass, what it gained, what it lost, what it rewrote, what it says —
    because a reader comparing two entries is comparing positions.
    """
    rows = []
    for st, name in sorted(ev['status']):
        rows.append('`%s` is %s' % (name, 'new' if st == 'NEW' else 'gone'))
    rows.extend(sig_line(row) for row in ev['sig'][:4])
    if len(ev['sig']) > 4:
        rows.append('(and %s)' % plural(len(ev['sig']) - 4, 'more signature '
                                        'change'))
    for key, one, many in (('+method', 'new method', 'new methods'),
                           ('+field', 'new field', 'new fields'),
                           ('-method', 'removed method', 'removed methods'),
                           ('-field', 'removed field', 'removed fields'),
                           ('~', 'body rewritten behind an unchanged signature',
                            'bodies rewritten behind unchanged signatures')):
        if not ev[key]:
            continue
        # Deduplicated for display only: two constructors differing solely in
        # their parameters both render as `<init>`, and a list reading
        # "`<init>`, `<init>`" tells the reader nothing except that something
        # is wrong with the tool. The count stays honest because it is taken
        # before the dedup.
        labels = sorted({member_label(s) for s in ev[key]})
        rows.append('%s: %s' % (plural(len(ev[key]), one, many),
                                ', '.join(bullets(['`%s`' % x for x in labels], 6))))
    for before, after in sorted(ev['rewords']):
        rows.append('reworded %s to %s' % (quote(before), quote(after)))
    # Whatever a rewording already named is not also an addition: listing it
    # under `loads` as well reports one edit twice and calls one of them new.
    said = {after for _, after in ev['rewords']}
    shown = [s for s in sorted(ev['loads']) if s not in said]
    if shown:
        rows.append('loads %s'
                    % ', '.join(bullets([quote(s) for s in shown], 8)))
    return rows


def attach_upcalls(topics, fs):
    """File each new native→Java call under the class it names.

    This is the strongest single line an entry can carry — the library states
    the method it intends to call, in bytes, and did not state it before — and
    as a finding of its own it reads as an unrelated fact about the dex rather
    than as the missing half of the fix above it.

    Every one of them has a home by construction, which is why the ledger can
    call this kind absorbed without checking: the finding and the topics are
    built from the same member rows, so the class naming an upcall is a class
    some topic holds. Matched on the top-level name, because that is the unit a
    topic is keyed by and a nested class's upcall belongs to the class that
    contains it.
    """
    by_class = {name: t for t in topics.values() for name in t.classes}
    for f in fs:
        if f.kind != UPCALL:
            continue
        for line in f.evidence:
            cls, _, desc = line.partition('.')
            # Indexed rather than searched, and unguarded rather than
            # defaulted: a class with no topic would be a broken invariant, and
            # a KeyError here says so where a quiet `if` would drop the
            # strongest line in the entry and leave the ledger claiming it had
            # been absorbed.
            by_class[outer(cls)].upcalls.append((outer(cls), desc))


def attach_rewordings(topics, fs):
    """File a reworded message under the class that loads it now.

    A message before and after is the most quotable line a note can carry, and
    the pair says more than either half: the entry above it already lists the
    new string among what its class loads, so left as a finding of its own this
    reports the same edit twice and calls one of them an addition.

    Placed by the string, not by the class the finding names — it names none.
    A rewording whose new text no app class is recorded as loading stays a
    finding of its own, and the ledger says so.
    """
    by_text = {}
    for t in topics.values():
        for cls, ev in t.classes.items():
            for lit in ev['loads']:
                by_text[lit] = (t, cls)
    placed = True
    for f in fs:
        if f.kind != 'REWORDED TEXT':
            continue
        for line in f.evidence:
            m = REWORD.match(line)
            home = by_text.get(m.group(2)) if m else None
            if home is None:
                placed = False
            else:
                topic, cls = home
                topic.rewords.append((cls, m.group(1), m.group(2)))
    return placed


def _native_gone(topic):
    """The log strings the library no longer contains."""
    if not topic.native_gone:
        return []
    return (['    - **Native.** the library no longer contains %s:'
             % plural(len(topic.native_gone), 'log string')]
            + ['        - `%s`' % s for s in bullets(topic.native_gone, 8)])


def render_topic(topic):
    """One entry: the lead sentence, then one sub-bullet per layer it touches.

    Native first, then classes by how much of each moved. That order is not
    cosmetic: a change that spans both halves reads as one thing when the
    library comes first and as two when it does not — which is the whole
    content of the `CROSS-LAYER CHANGE` finding.
    """
    out = ['- **%s**' % lead(topic)]
    for cls, desc in topic.upcalls[:4]:
        out.append('    - **Native.** the library now calls `%s.%s`, a name it '
                   'did not contain before.' % (cls, desc))
    for s, succ in topic.stubs[:3]:
        out.append('    - **Native.** `%s` is no longer in the library%s'
                   % (s, (', and %s is new'
                          % ', '.join('`%s`' % x for x in succ))
                      if succ else '; nothing obviously replaced it'))
    if not topic.native:
        out.extend(_native_gone(topic))
    if topic.native:
        fails = [s for k, s in topic.native if k == 'fail']
        plain = [s for k, s in topic.native if k != 'fail']
        out.extend(_native_gone(topic))
        out.append('    - **Native.** the library gained %s, %s on a failure '
                   'path:' % (plural(len(topic.native), 'log string'),
                              '1 of them' if len(fails) == 1
                   else '%d of them' % len(fails)))
        # Every string, then one cap over the lot: capping the two lists
        # separately dropped whatever fell off the end of each without the
        # count that follows saying so.
        out.extend('        - `%s`' % s for s in bullets(fails + plain, 10))
    order = sorted(topic.classes,
                   key=lambda c: (-_class_weight(topic.classes[c]), c))
    for cls in order:
        rows = class_rows(topic.classes[cls])
        # One class with one thing to say is a sentence, not a list. A nested
        # bullet holding a single line is a level of indentation carrying no
        # information, and a page of them reads as a form rather than a note.
        if len(rows) == 1:
            out.append('    - **`%s`.** %s.' % (cls, rows[0]))
            continue
        out.append('    - **`%s`.**' % cls)
        out.extend('        - %s' % r for r in rows)
    out.extend('    - %s' % x for x in topic.extra)
    return out


def _class_weight(ev):
    return sum(len(v) for k, v in ev.items() if k != 'status')


def scope(e, fs):
    """The paragraph every build section opens with.

    What differs, how much of the archive that is, and where the code change
    landed — the reader's orientation, and the honest bound on everything the
    section goes on to claim.
    """
    groups, abis = Counter(), Counter()
    for st in ('added', 'removed', 'changed'):
        for name in e.entries[st]:
            base = name.rsplit('/', 1)[-1]
            groups[base] += 1
            abis[base] += name.startswith('lib/')
    parts = []
    for base, n in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
        # Several copies of one basename under lib/ is one library built for
        # each ABI — the same source, not several changes, and worth naming as
        # such because a reader who sees "4 copies" wonders which four.
        parts.append('`%s`' % base if n == 1
                     else '%d %s of `%s`'
                     % (n, 'ABIs' if abis[base] == n else 'copies', base))
    if len(groups) > 8:
        parts.append('and %s' % plural(len(groups) - 8, 'other entry',
                                       'other entries'))
    moved = sum(len(v) for v in e.entries.values())
    line = ('%d of %d archive entries differ: %s.'
            % (moved, e.total_entries, ', '.join(parts)) if parts
            else 'No archive entry differs.')
    area = [f for f in fs if f.kind == 'CHANGE AREA']
    if area:
        line += ' Every changed class is in `%s`.' % area[0].title.split()[-1]
    return line


def character(e, fs, topics):
    """One phrase for what kind of build this is.

    A guess, and a cheap one: the section counts and the confinement of the
    change, nothing else. It leads the paragraph because that is where the
    notes file puts it, and it is the first thing a writer should overrule.
    """
    sections = Counter(t.section() for t in topics.values())
    if not topics:
        return 'A packaging-only build: nothing in the code differs'
    confined = any(f.kind == 'CHANGE AREA' for f in fs)
    if sections['New'] == 0 and sections['Changed'] == 0 and confined:
        return 'A single-purpose bugfix build'
    if sections['New'] == 0 and sections['Changed'] == 0:
        return 'A bugfix build'
    if sections['New'] or sum(t.counts()['+method'] for t in topics.values()) > 20:
        return 'A feature build'
    return 'A maintenance build'


def ledger(fs, placed):
    """Every stage-7 finding, and which section of the draft absorbed it.

    The one mechanical guarantee this stage can offer: a finding cannot fall
    out of the draft unnoticed, because the count at the bottom is of the
    findings themselves and not of the ones that made it. Cutting a line
    afterwards stays a decision; losing one does not happen.
    """
    out = ['```', 'evidence ledger — every stage-7 finding and where it went',
           '(absorbed = its content is inside an entry above, under the change '
           'it belongs to)', '']
    for f in sorted(fs, key=lambda f: (f.rank, f.kind)):
        out.append('  %-24s %-9s %s' % (f.kind, placed.get(f.kind, 'Notes'),
                                        f.title))
    out.append('')
    out.append('  %s, all accounted for above.' % plural(len(fs), 'finding'))
    out.append('```')
    return out


def version_code(e):
    """The heading, which is the build's own identity.

    A run whose manifest could not be read has none, and saying so is better
    than heading the section with whatever was nearest: a note filed under the
    wrong build is worse than one filed under an obvious blank. Derived here
    rather than at each use, because the draft's heading and the section the
    insert refuses to overwrite have to be the same string.
    """
    return e.about.get('versionCode') or '(unknown versionCode)'


def build(e, fs):
    """The draft, as a list of lines."""
    topics = dex_topics(e)
    attach_native(e, topics)
    attach_upcalls(topics, fs)
    rewords_placed = attach_rewordings(topics, fs)
    for topic in topics.values():
        for cls, before, after in topic.rewords:
            topic.classes[cls]['rewords'].append((before, after))

    # Findings with no class behind them: the manifest, the resource table,
    # the archive entries nothing else read. They are entries in their own
    # right — an app that asks for a new permission has changed for its user
    # whether or not a class moved.
    loose, placed = [], {}
    for f in fs:
        if f.kind in ABSORBED or (f.kind == 'REWORDED TEXT' and rewords_placed):
            placed[f.kind] = 'absorbed'
            continue
        if f.kind in CAVEAT or f.kind in NEGATIVE:
            placed[f.kind] = 'Notes'
            continue
        section = ('New' if f.kind in ('PERMISSION CHANGE', 'NEW COMPONENT',
                                       'NEW INTENT FILTER', 'NEW RESOURCE',
                                       'NEW NATIVE EXPORT')
                   else 'Notes' if f.kind == 'OTHER FILES'
                   else 'Changed')
        placed[f.kind] = section
        loose.append((section, f))

    out = [BEGIN, '## %s' % version_code(e), '']
    out.append('%s. %s' % (character(e, fs, topics), scope(e, fs)))
    out.append('')
    by_section = defaultdict(list)
    for t in sorted(topics.values(), key=Topic.weight, reverse=True):
        by_section[t.section()].append(render_topic(t))
    for section, f in loose:
        by_section[section].append(
            ['- **%s.**' % (f.title[0].upper() + f.title[1:])]
            + ['    - %s' % x for x in bullets(f.evidence, 8)])
    for section in ('New', 'Changed', 'Fixed'):
        if by_section[section]:
            out.append('### %s' % section)
            out.append('')
            for entry in by_section[section]:
                out.extend(entry)
                out.append('')
    notes = [f for f in fs if f.kind in CAVEAT or f.kind in NEGATIVE] \
        + [f for s, f in loose if s == 'Notes']
    out.append('### Notes')
    out.append('')
    for f in notes:
        out.append('- **%s** %s' % (f.kind.title() + ':', f.title))
        out.extend('    - %s' % x for x in bullets(f.evidence, 6))
    out.append('- **Not a change.** R8 re-sharding, renumbered lambdas, '
               'accessor renames and reflowed `.rodata` are suppressed by the '
               'tool and produce no findings; nothing above rests on them.')
    out.append('')
    out.append(END)
    out.append('')
    out.extend(ledger(fs, placed))
    return out


PREAMBLE = [
    'Draft release note — stage 8. Everything between the two markers is the',
    'section to paste into docs/<package>-release-notes.md; everything outside',
    'them, including the ledger at the bottom, is scaffolding.',
    '',
    'Written by rule, not by reading: the sections are a guess from the shape',
    'of the evidence, and every lead sentence says what the build contains',
    'rather than what it does for the person using the app. Rewrite the bold',
    'sentences, merge what is one change, and move what turns out to be noise',
    'into Notes saying so. Dex and resource evidence is exact and can be',
    'stated flatly; the native lines are log strings out of a stripped',
    'library, which say what the binary contains and nothing about where or',
    'why — keep them hedged.',
    '',
]


def insert(path, section, code):
    """Put a finished section into the notes file, newest first.

    Refuses rather than overwrites: a section for this build already in the
    file is either the one somebody has edited or a sign that the run was
    repeated, and replacing it silently would discard writing this tool cannot
    reproduce.
    """
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if re.search(r'^## %s\s*$' % re.escape(code), text, re.M):
        raise SystemExit('notes.py: %s already has a section for %s' % (path, code))
    head, sep, rest = text.partition('\n---\n')
    if not sep:
        raise SystemExit('notes.py: %s has no "---" separator to insert after' % path)
    body = '\n'.join(section).strip('\n')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(head + sep + '\n' + body + '\n\n---\n' + rest.lstrip('\n'))


def main(argv):
    into = None
    if len(argv) == 5 and argv[3] == '--into':
        into = argv[4]
    elif len(argv) != 3:
        sys.stderr.write('usage: notes.py WORKDIR REPORTDIR [--into NOTESFILE]\n')
        return 2
    e = Evidence(argv[1], argv[2])
    fs = [f for d in DETECTORS for f in d(e)]
    draft = build(e, fs)
    text = '\n'.join(PREAMBLE + draft) + '\n'
    path = os.path.join(argv[2], '8-notes.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    if into:
        body = draft[draft.index(BEGIN) + 1:draft.index(END)]
        insert(into, body, version_code(e))
        sys.stdout.write('inserted the %s section into %s\n'
                         % (version_code(e), into))
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stdout.write('draft written to %s\n' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
