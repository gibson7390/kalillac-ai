"""Structured search intent for one Space message: the authoritative decision.

Inside a Space, whether to search, what to search for, and whether to ask
for a subject are decided from the current message alone -- never from
saved history, the owner's preferences, other conversations or the Space
facts. `parse(message).decision` is one of:

    "search"     an explicit lookup with a subject: search, with exactly the
                 original message as the query
    "clarify"    a lookup with no resolvable subject: the fixed
                 clarification, no provider call, no model call
    "no_search"  the message uses search vocabulary but is not a lookup
                 (an ordinary message, or a capability question): never
                 search, whatever any other classifier says
    None         no decision: the message has no search command, no
                 attribute question and no search vocabulary; the
                 application's independent live-information channels may
                 still decide (see app_fastapi_candidate._space_route_search)

Analysis copy. The message is canonically composed (NFC: e + U+0301 -> é);
then each character is NFKC-normalized and case-folded on its own; Unicode
apostrophes become "'"; runs of whitespace become one space. Every analysis
character remembers whether the (NFC) character it came from is a Unicode
letter -- compatibility expansion ("™" -> "tm") never creates that evidence. Punctuation is kept -- it is grammar. The
original message is what the model and the search provider receive,
untouched.

Grammar of an explicit search command (positions matter):

    command   := polite* verb [provider] [connector] target
    sep       := " " | [" "] ("," | ":") [" "]
    polite    := ("please" | "can you" | "could you" | "would you" | "will you"
                  | "kalillac") sep                     -- repeatable
    verb      := "search" ["up"] | "look up" | "look it up" | "find out" | "google" | "bing"
                 | "look" (only with a provider: "look online for ...")
                 | "find" (only with a provider, or before a wh-word or an
                   information noun: "find where ...", "find news about ...")
    provider  := ["on" | "in" | "using" | "with" | "via"] ["the"]
                 ("web" | "internet" | "online" | "google" | "bing" | "brave"
                  | "duckduckgo")
    connector := "for" | "about" | "up"
    target    := everything after the above, verbatim

Each of verb, provider and connector ends either at a `sep` followed by more
text, or at the end of the message -- where "end" also means that only
punctuation, symbols or spaces remain ("Search;", "Search Google."). A
semicolon, em dash or en dash between command parts, spaced or not
("Search; x", "Search Google ; x", "Search — x", "Search for— x"), or any
other punctuation directly after a provider or connector ("Search Google;
for x"), is a boundary the grammar does not accept: fail closed. A dot
directly followed by a letter or digit is part of a domain ("google.com"),
not a boundary.

A provider word is command syntax only in the provider position (or as the
verb itself, "google his biography"); anywhere in the target it is an
ordinary word that can be the subject ("Search for Google and its CEO").

Targetless commands. A command whose target is empty:
  - after a question prefix (can / could / would / will you) and no
    connector -- "Can you search the web?" -- is a capability question:
    "no_search";
  - otherwise -- "Search", "Search Google;", "Please search", "Look up",
    "Search for.", "Search:" -- is an imperative with nothing to look up:
    "clarify".

Fail closed ("clarify"): a message that starts (after polite words and any
punctuation) with search / search up / look up / look it up / find / find
out / google / bing, but that the grammar cannot resolve:
  - the grammar found no verb at all, and the command word ends at a
    boundary other than a hyphen or apostrophe ("Please -- search Google
    for ...", "Search; his biography"); or
  - no verb survived, and the command word is followed directly by
    punctuation other than a space, comma, colon, hyphen or apostrophe
    ("Look up; Microsoft").
Command words, provider names, polite words and separators are never the
subject. "Search-engine tips" and "Google's market share" are not commands.

An attribute question -- "Where is he from?", "When was it founded?" -- is a
lookup whose target is the whole message: "clarify" without a subject,
otherwise no decision (the independent verification channel decides).

Search vocabulary ("no_search" when present and nothing above applies): the
tokens search, searches, searched, searching, lookup, googled, googling, and
the phrases "look up" / "look it up".

Tokens: maximal runs of letters (L*) and decimal digits (Nd); a combining
mark (M*) continues the token it follows; an apostrophe is kept between two
word characters ("o'connor"); a trailing "'s" (possessive) is removed.
Punctuation, symbols, emoji and whitespace are never part of a token.

Subject evidence. A token counts towards the subject only if at least one of
its characters comes from an original Unicode letter. Digits with marks
("1️⃣") and symbols that NFKC expands into letters ("™" -> "tm") are
therefore never a subject.

Subject. Substantive tokens are evidenced tokens that are not function
words, question words, pronouns, numbers or attribute words that cannot
stand alone (full, current, biography, profile, information, details, born,
founded, history, latest, updates, ...). Pronoun-headed targets fail closed:
if the target contains a personal or possessive pronoun, only substantive
tokens BEFORE the first pronoun can be its referent. "Search for Satya
Nadella and where he is from" names a subject; "Search for his biography in
Tokyo, Japan", "... during 2025", "... (Japan)" and "Search for his detailed
biography" do not -- nothing after an unresolved pronoun can resolve it.

Bounds: a message longer than USER_MESSAGE_MAX (which the API never lets
through) fails closed. The analysis copy and tokenization are single linear
passes; every regex is anchored and built from literal alternatives without
nested ambiguity.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .config import USER_MESSAGE_MAX

PRONOUNS = frozenset("he him his she her hers they them their theirs it its".split())

_PROVIDERS = ("the web", "the internet", "web", "internet", "online", "google", "bing", "brave", "duckduckgo")
_VERBS = ("search up", "search", "look it up", "look up", "look", "find out", "find", "google", "bing")
# "find" and "look" are ordinary verbs ("find the bug", "look at this") unless
# a provider follows, or -- for "find" -- a wh-word or an information noun.
_FIND_LOOKUP = re.compile(
    r"^(?:where|when|who|whom|whose|what|which|why|how|information|info|news|articles|sources|results|details)\b")
_POLITE_WORDS = r"(?:please|can you|could you|would you|will you|kalillac)"
# Literal alternatives and a separator that cannot overlap them: linear.
_POLITE = re.compile(r"^(?:" + _POLITE_WORDS + r"(?: ?[,:] ?| ))+")
_QUESTION = re.compile(r"\b(?:can|could|would|will) you\b")
# After any polite words and any punctuation, a command word ending at a
# boundary other than a hyphen, an apostrophe or a domain dot
# ("search-engine", "google's", "google.com"). Used only when the grammar
# found no verb at all.
_COMMAND_WORDS = r"(?:search(?: up)?|look(?: it)? up|find(?: out)?|google|bing)"
_COMMAND_START = re.compile(r"^(?:" + _POLITE_WORDS + r"\W*)*" + _COMMAND_WORDS + r"(?=(?!\.\w)[^\w'-]|$)")
# The same, followed directly by punctuation the grammar does not accept
# ("look up; microsoft"): used whenever no verb survives parsing.
_COMMAND_BAD_SEPARATOR = re.compile(r"^(?:" + _POLITE_WORDS + r"\W*)*" + _COMMAND_WORDS + r"(?=(?!\.\w)[^\w\s,:'-])")

SEARCH_VOCABULARY = frozenset("search searches searched searching lookup lookups googled googling".split())
_VOCABULARY_PHRASES = (("look", "up"), ("look", "it", "up"))

# Words that cannot be a subject on their own.
FUNCTION_WORDS = frozenset("""
    a an the and or but nor so of to in on at by for from with about into onto over under up down out off
    as than then that this these those there here what's who's where's when's how's what who whom whose
    which where when why how is are was were be been being am do does did done has have had having will
    would shall should can could may might must not no yes me my mine we us our you your yours i
    please any some all each every more most much many also just only very really
""".split())
ATTRIBUTE_WORDS = frozenset("""
    full current biography bio profile information info details detail born birth birthplace birthday
    founded founding founder founders history latest updates update news recent new age old height
    nationality net worth wife husband spouse married children family background career life death died
    from based located headquartered location live lives lived
    ceo cto cfo coo president founder owner owners leader chairman chair head director manager partner
    address website site phone email contact price stock value revenue salary worth stats statistics
""".split())


def _is_attribute(token: str) -> bool:
    """An attribute word, singular or a simple plural ("biographies", "updates")."""
    if token in ATTRIBUTE_WORDS:
        return True
    if token.endswith("ies") and token[:-3] + "y" in ATTRIBUTE_WORDS:
        return True
    if token.endswith("es") and token[:-2] in ATTRIBUTE_WORDS:
        return True
    return token.endswith("s") and token[:-1] in ATTRIBUTE_WORDS


_ATTRIBUTE_QUESTION = re.compile(
    r"^(?:where|when|who|what|how old|how tall|which)\b.*\b(?:from|born|based|founded|located|headquartered|"
    r"age|old|tall|biography|nationality|net worth|died|live|lives|lived|ceo|founder|birthday)\b"
)

DECISIONS = ("search", "clarify", "no_search", None)


@dataclass(frozen=True)
class SearchIntent:
    decision: str | None            # "search" | "clarify" | "no_search" | None (see the module docstring)
    explicit_command: bool          # the message is a search command (possibly targetless)
    attribute_lookup: bool          # an entity-attribute question
    verb: str | None
    provider: str | None
    target: str                     # target phrase in the analysis copy
    pronouns: tuple[str, ...]       # pronouns in the target
    subject: tuple[str, ...]        # substantive, letter-evidenced words naming the subject

    @property
    def is_lookup(self) -> bool:
        return self.explicit_command or self.attribute_lookup

    @property
    def needs_subject(self) -> bool:
        """A lookup whose target names nothing. Never resolved from history."""
        return self.decision == "clarify"


_UNRESOLVED = SearchIntent("clarify", True, False, None, None, "", (), ())


def _analysis(message: str) -> tuple[str, list[bool]]:
    """(analysis text, per-character "comes from an original letter")."""
    chars: list[str] = []
    evidence: list[bool] = []
    # Canonical composition first (e + U+0301 -> é): it only joins a base
    # character with its marks, so it never turns a non-letter into a letter.
    for original in unicodedata.normalize("NFC", str(message)):
        letter = unicodedata.category(original)[0] == "L"
        for ch in unicodedata.normalize("NFKC", original).casefold():
            if ch in "’‘ʼ":
                ch = "'"
            if ch.isspace():
                if not chars or chars[-1] == " ":
                    continue
                ch = " "
            chars.append(ch)
            evidence.append(letter)
    while chars and chars[-1] == " ":
        chars.pop()
        evidence.pop()
    return "".join(chars), evidence


def normalize(message: str) -> str:
    return _analysis(message)[0]


def _is_word_char(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category[0] == "L" or category == "Nd"


def _token_spans(text: str, evidence: list[bool], start: int = 0) -> list[tuple[str, bool]]:
    """(token, letter-evidenced) for each token of text[start:]. One linear pass."""
    out: list[tuple[str, bool]] = []
    word: list[str] = []
    seen_letter = False
    n = len(text)

    def end_word():
        nonlocal seen_letter
        if word:
            token = "".join(word).rstrip("'")
            if token.endswith("'s"):
                token = token[:-2]
            if token:
                out.append((token, seen_letter))
            word.clear()
        seen_letter = False

    for i in range(start, n):
        ch = text[i]
        if _is_word_char(ch) or (word and unicodedata.category(ch)[0] == "M"):
            word.append(ch)                     # a combining mark continues its word
            seen_letter = seen_letter or evidence[i]
        elif ch == "'" and word and i + 1 < n and _is_word_char(text[i + 1]):
            word.append(ch)                     # o'connor, nadella's
        else:
            end_word()
    end_word()
    return out


def _tokens(text: str) -> list[str]:
    """Tokens of an analysis string (evidence not considered)."""
    return [token for token, _ in _token_spans(text, [True] * len(text))]


def _substantive(token: str, evidenced: bool) -> bool:
    return (evidenced and token not in PRONOUNS and token not in FUNCTION_WORDS
            and not _is_attribute(token) and not token.isdigit())


def _subject(spans: list[tuple[str, bool]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(pronouns, subject). Only substantive tokens before the first pronoun
    can be its referent; with no pronoun, every substantive token counts."""
    pronouns = tuple(token for token, _ in spans if token in PRONOUNS)
    subject = []
    for token, evidenced in spans:
        if token in PRONOUNS:
            break
        if _substantive(token, evidenced):
            subject.append(token)
    return pronouns, tuple(subject)


def _at_end(after: str) -> bool:
    """Only punctuation, symbols or spaces remain."""
    return not any(_is_word_char(ch) for ch in after)


def _take(text: str, options) -> tuple[str | None, str, bool]:
    """(option, rest, separated by a comma or colon) for the first option
    that `text` starts with, ending at the end or at a `sep`."""
    for option in options:
        if not text.startswith(option):
            continue
        after = text[len(option):]
        if _at_end(after):
            return option, "", any(ch in ",:;" for ch in after)
        if after[0] in " ,:":
            rest = after.lstrip(" ,:")
            return option, rest, any(ch in ",:" for ch in after[:len(after) - len(rest)])
    return None, text, False


_BAD_SEPARATOR = re.compile(r"(?!\.\w)[^\w\s,:'-]")   # "." inside a domain is not a separator


def _bad_boundary(text: str, options) -> bool:
    """`text` starts with one of `options` followed directly by punctuation
    the grammar does not accept, with more text after it ("google; for x")."""
    for option in options:
        if text.startswith(option):
            after = text[len(option):]
            if after and _BAD_SEPARATOR.match(after) and not _at_end(after):
                return True
    return False


# Separators the grammar never accepts, spaced or not: "search ; x", "search — x".
_UNACCEPTED_SEPARATORS = ";—–"


def _unaccepted_next(rest: str) -> bool:
    return bool(rest) and rest[0] in _UNACCEPTED_SEPARATORS and not _at_end(rest)


def _has_vocabulary(tokens: list[str]) -> bool:
    if any(token in SEARCH_VOCABULARY for token in tokens):
        return True
    return any(tuple(tokens[i:i + len(phrase)]) == phrase
               for phrase in _VOCABULARY_PHRASES for i in range(len(tokens) - len(phrase) + 1))


def parse(message: str) -> SearchIntent:
    if len(str(message)) > USER_MESSAGE_MAX:
        return _UNRESOLVED                      # never reached through the API; fail closed
    normalized, evidence = _analysis(message)
    polite = _POLITE.match(normalized)
    offset = polite.end() if polite else 0
    question = bool(polite and _QUESTION.search(normalized[:offset]))
    text = normalized[offset:]
    verb, rest, separated = _take(text, _VERBS)
    if verb is None and _COMMAND_START.match(normalized):
        return _UNRESOLVED                      # a search command the grammar cannot resolve
    provider = None
    connector = None
    if verb is not None and _unaccepted_next(rest):
        return _UNRESOLVED                      # "search ; x", "search — x"
    if verb is not None:
        if verb in ("google", "bing"):
            # "Google his biography": the engine is the verb.
            provider = verb
        else:
            if _bad_boundary(rest, _PROVIDERS) or _bad_boundary(rest, ("on", "in", "using", "with", "via")):
                return _UNRESOLVED              # "search google; for x": a provider never becomes the subject
            preposition, after, _ = _take(rest, ("on", "in", "using", "with", "via"))
            if preposition is not None and _bad_boundary(after, _PROVIDERS):
                return _UNRESOLVED
            name, after_name, sep = _take(after, _PROVIDERS)
            if name is not None:
                provider, rest, separated = name, after_name, separated or sep
            elif preposition is None:
                name, after_name, sep = _take(rest, _PROVIDERS)
                if name is not None:
                    provider, rest, separated = name, after_name, separated or sep
        if verb == "look" and provider is None:
            verb = None                         # "look at this code" is not a search
        elif verb == "find" and provider is None and not _FIND_LOOKUP.search(rest):
            verb = None                         # "find the bug in my function" is not a search
    if verb is None and _COMMAND_BAD_SEPARATOR.match(normalized):
        return _UNRESOLVED                      # "look up; microsoft": an unresolvable command
    if verb is not None:
        if _bad_boundary(rest, ("for", "about", "up")) or _unaccepted_next(rest):
            return _UNRESOLVED                  # "search for; x", "search google — x"
        connector, rest, sep = _take(rest, ("for", "about", "up"))
        separated = separated or sep
        if _unaccepted_next(rest):
            return _UNRESOLVED                  # "search for — x"
        target = rest
    else:
        target = text
    start = len(normalized) - len(target)
    spans = _token_spans(normalized, evidence, start)
    pronouns, subject = _subject(spans)
    explicit = verb is not None
    attribute = verb is None and bool(_ATTRIBUTE_QUESTION.search(text))
    if explicit:
        if not spans and question and not connector and not separated:
            decision = "no_search"              # "Can you search the web?": a capability question
        else:
            decision = "search" if subject else "clarify"
    elif attribute:
        decision = None if subject else "clarify"
    elif _has_vocabulary([token for token, _ in _token_spans(normalized, evidence)]):
        decision = "no_search"                  # search vocabulary, but not a lookup
    else:
        decision = None
    return SearchIntent(
        decision=decision,
        explicit_command=explicit and decision != "no_search",
        attribute_lookup=attribute,
        verb=verb, provider=provider, target=target, pronouns=pronouns, subject=subject,
    )
