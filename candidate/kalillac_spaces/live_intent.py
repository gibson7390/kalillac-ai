"""Live-information intent for one Space message.

An independent search channel: it runs only when the structured search
parser (kalillac_spaces.search_intent) has no decision, and it never
matches command words. It answers one question about the current message
alone: is this an actual question or request whose answer is inherently
time-sensitive or likely to have changed? All three must hold:

  1. Request structure. After an optional "please", the message starts with
     an interrogative (what, who, when, where, which, how, is, are, does,
     do, has, have, will -- and their contractions) or a request prefix
     (tell me, show me, give me, get me, let me know, do you know, can /
     could / would / will you [please] tell me / check / find out / let me
     know, I want / need / would like to know, check if / whether), or it
     ends with a question mark.

  2. Freshness. The request asks for the present state of something:
       - a freshness word: today, today's, tonight, tomorrow, current,
         currently, latest, newest, recent, upcoming, breaking, or a
         freshness phrase: right now, at the moment, as of now, so far,
         most recent, up to date, year to date;
       - a present-relative period: "this" + week, weekend, morning,
         afternoon, evening, month, quarter, season or year ("so far this
         season", "this year"); a past period such as "during the 2020
         season" has no "this" and is not one;
       - "next" followed within three words by a scheduled-event noun (game,
         match, election, launch, release, event, race, fight, episode,
         season, eclipse, flight, concert, tour, meeting, holiday);
       - "live" followed by score, scores, status, results, updates or
         coverage;
       - or, unless the question is about the past (what / who / when /
         where / which / how + was / were / did / had; "history",
         "historical"; or an explicit past date -- "July 14, 1980", "14 July
         1980", "1980-07-14" before today -- or a year before this year,
         "the 1990 World Cup final"), an inherently changing data category:
         stock price,
         share price, price, prices, weather, forecast, standings, exchange
         rate, headlines, outage; a sports score -- "score(s)" together
         with sports or event terminology (game, match, team, league,
         playoff, final, tournament, cup, inning, halftime, overtime, NBA,
         NFL, MLB, NHL, WNBA, MLS, NCAA) -- since "score" alone may be a
         musical or test score; or a status question "is / are X down /
         offline / up". Explicit freshness above always wins over a date or
         year ("the latest update for Windows Server 2022"); "today" is the
         UTC date unless the caller passes one.

  3. A subject. At least one letter-evidenced substantive word that is not
     a freshness word (search_intent's tokens and evidence), or a pronoun
     -- in which case the router asks for the subject instead of searching.

Never live information (checked first; each on its own suffices):
  - code or data: backticks, braces or brackets, "=>", "==", a call such
    as name(...), a JSON key ("x": ...), or programming keywords (def,
    function, const, let, var, return, import, class, print, console);
  - quoted text: anything inside “…”, "…", ‘…’, «…» or '…' is removed
    before the steps above, so a freshness word in a quotation counts for
    nothing;
  - writing or creative requests: write, draft, compose, rewrite, edit,
    proofread, translate, paraphrase, summarize, summarise, generate,
    create, imagine, pretend, continue, finish;
  - a language question -- a whole-message decision (_is_language_question):
    the complete request, on a copy that keeps every quotation mark and
    apostrophe as typed, must be exactly one of the forms below: a TERM (a
    quoted span of up to 60 characters between a matching pair -- “…”,
    "…", «…», ‘…’ or '…', where inside a single-quote pair an apostrophe
    followed by a letter or digit belongs to the term, as in ‘isn’t’ or
    'O'Connor' -- or one bare word, which may contain apostrophes between
    letters), the grammar around it, and at most one
    qualifier (today, right now, now, currently, in [American / British]
    English, in this sentence). Nothing else may follow, so language words
    in a product name, a coordinated subject ("the Word web and mobile
    apps"), a quoted error message, supporting context or a second sentence
    never veto a live request:
      - "is / are / was / were / isn't / aren't / can [the word / term /
        phrase / expression] TERM[, and / or TERM ...] [a / an] <part of
        speech>[s / phrase] [or a / an <part of speech>]" ("Is “run” a
        noun?", "Are “current” and “recent” adjectives?");
      - "what does / do / did [the word ...] TERM mean"; "what is / what's
        the meaning / definition / spelling / pronunciation / etymology /
        plural / past tense of TERM"; "define / spell / pronounce TERM";
      - "how do / does / ... you / I / ... spell / pronounce TERM", "how to
        spell / pronounce TERM", "how is / are ... TERM spelled / spelt /
        pronounced";
      - "compare / contrast / what is the difference between the words
        TERM and / or / vs TERM";
      - "what are / is [the / some] synonyms / antonyms of / for TERM",
        "what rhymes with TERM";
  - definitional or conceptual questions: explain, describe, define, what
    is a / an, what's a / an, what are some;
  - programming objects: directory, folder, variable, function, array,
    loop, pointer, index, iteration, cursor, thread, branch, commit;
  - questions about the assistant: after removing any request prefix, the
    underlying question mentions the assistant or its memory anywhere --
    you, your, yours, yourself, you'll, you're, you've, you'd, Kalillac,
    remember, retain, forget (and their -s / -ed forms). "Can you tell me
    whether you will remember this tomorrow?" is about Kalillac; "Can you
    tell me tomorrow's weather in Terre Haute?" is not.

Bounded: linear passes over a message the API already limits to
USER_MESSAGE_MAX characters; regexes are anchored or built from literal
alternatives.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
import unicodedata

from .config import USER_MESSAGE_MAX
from .search_intent import FUNCTION_WORDS, PRONOUNS, _analysis, _is_attribute, _token_spans, _tokens

# Each quotation and call alternative stops at the next opening mark, so
# every scan is linear.
_QUOTED = re.compile(r"“[^“”]*”|\"[^\"]*\"|‘[^‘’]*’|«[^«»]*»|(?<!\w)'[^']*'(?!\w)")
_CODE = re.compile(r"`|[{}\[\]]|=>|==|\b[a-z_][a-z0-9_.]*\([^()]*\)|\"\w+\"\s*:|"
                   r"\b(?:def|function|const|let|var|return|import|class|print|console)\b")
_POLITE = re.compile(r"^(?:please[ ,:]+)+")
_INTERROGATIVE = re.compile(
    r"^(?:what|what's|whats|who|who's|whose|when|when's|where|where's|which|how|how's|"
    r"is|isn't|are|aren't|does|do|has|have|will)\b")
_REQUEST = re.compile(
    r"^(?:tell me|show me|give me|get me|let me know|do you know|"
    r"(?:can|could|would|will) you (?:please )?(?:tell me|check|find out|let me know)|"
    r"i want to know|i need to know|i'd like to know|i would like to know|check (?:if|whether))\b[ ,:]*")
# Language questions: a whole-message decision. The complete request (the
# analysis copy WITH its quotation marks, after "please") must be exactly
# one language question below -- the term, the grammar around it and at
# most one bounded qualifier, nothing else. Language vocabulary inside a
# product name, a coordinated subject, a quoted error message, supporting
# context or a second sentence can therefore never veto a live request.
_POS = r"(?:adjective|adverb|noun|verb|pronoun|preposition|conjunction|article|interjection)"
# A term: a quoted span (1-60 characters, delimiter-aware) or one bare word.
# Each quote pair closes only on its own closing mark. Inside single-quote
# pairs ‘…’ and '…', an apostrophe (' or ’) followed by a letter or digit is
# part of the term ("isn’t", "O'Connor"); the closing mark is the one that is
# not. Unmatched or crossed delimiters ("“isn’t'") never form a term. Every
# alternative is a disjoint character class, so the scan is linear.
_QUOTED_TERM = (r"“[^“”]{1,60}”|\"[^\"]{1,60}\"|«[^«»]{1,60}»|"
                r"‘(?:[^‘’']|['’](?=\w)){1,60}’(?!\w)|"
                r"'(?:[^'‘’]|['’](?=\w)){1,60}'(?!\w)")
_BARE = r"[^ ?.!,“”\"«»‘’']"
# A bare word may contain apostrophes between letters ("isn't", "O’Connor").
_TERM = r"(?:" + _QUOTED_TERM + r"|" + _BARE + r"+(?:['’]" + _BARE + r"+)*)"
_TERMS = _TERM + r"(?:(?:,| and| or|, and|, or) " + _TERM + r"){0,3}"
_NAMED = r"(?:the (?:word|term|phrase|expression)s? )?"
_QUALIFIER = r"(?: (?:today|right now|now|currently|in (?:american |british )?english|in this sentence))?"
_END = _QUALIFIER + r"[ ?.!]*"
_LANGUAGE_QUESTIONS = tuple(re.compile(pattern) for pattern in (
    # "Is “run” a noun?", "Is the word “run” a noun?", "Are “current” and “recent” adjectives?"
    r"(?:is|are|was|were|isn't|aren't|can) " + _NAMED + _TERMS + r" (?:(?:a|an) )?" + _POS
    + r"(?:s| phrase| phrases)?(?: or (?:a|an) " + _POS + r")?" + _END,
    # "What does the word “current” mean?", "What is the meaning of “current”?"
    r"what (?:does|do|did) " + _NAMED + _TERM + r" mean" + _END,
    r"(?:what is|what's) the (?:meaning|definition|spelling|pronunciation|etymology|plural|past tense) of "
    + _NAMED + _TERM + _END,
    r"(?:define|spell|pronounce) " + _NAMED + _TERMS + _END,
    # "How do you spell “necessary”?", "How is “necessary” spelled?"
    r"how (?:do|does|did|would|should|can) (?:you|i|we|one|people|they) (?:spell|pronounce) " + _NAMED + _TERM + _END,
    r"how to (?:spell|pronounce) " + _NAMED + _TERM + _END,
    r"how (?:is|are|was|were) " + _NAMED + _TERM + r" (?:spelled|spelt|pronounced)" + _END,
    # "Compare the words current and recent."
    r"(?:compare|contrast|what is the difference between|what's the difference between) the "
    r"(?:word|term|phrase|expression)s " + _TERM + r" (?:and|or|vs\.?|versus|with) " + _TERM + _END,
    # "What are synonyms for current?", "What rhymes with latest?"
    r"what (?:are|is) (?:the |some )?(?:synonyms?|antonyms?) (?:of|for) " + _NAMED + _TERM + _END,
    r"what (?:rhymes|words rhyme) with " + _TERM + _END,
))


def _is_language_question(text: str) -> bool:
    """True only when the complete request is a language question."""
    return any(pattern.fullmatch(text) for pattern in _LANGUAGE_QUESTIONS)


def _language_copy(raw: str) -> str:
    """The request for the language decision: NFKC, case-folded, whitespace
    collapsed, "please" removed -- with every quotation mark and apostrophe
    kept exactly as typed, so quote pairs can be matched."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw).casefold()).strip()
    return _POLITE.sub("", text)
_MONTHS = (r"(january|february|march|april|may|june|july|august|september|october|november|december|"
           r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")
_MONTH_NUMBER = {name: number for number, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"),
    ("november", "nov"), ("december", "dec")), start=1) for name in names}
_DATE_MDY = re.compile(r"\b" + _MONTHS + r"\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)? (?:of )?" + _MONTHS + r"\.?,? (\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_WRITING = re.compile(
    r"^(?:write|draft|compose|rewrite|edit|proofread|translate|paraphrase|summarize|summarise|generate|"
    r"create|imagine|pretend|continue|finish)\b")
_CONCEPTUAL = re.compile(r"^(?:explain|describe|define|what is an?|what's an?|what are some)\b")
_PAST = re.compile(r"^(?:what|who|when|where|which|how)\s+(?:was|were|did|had)\b")
_STATUS = re.compile(r"^(?:is|are)\b.*\b(?:down|offline|up)\b")

# A request whose underlying question (after any request prefix such as
# "can you tell me") refers to the assistant or its memory is about
# Kalillac, not the world.
SELF_REFERENCE = frozenset("""
    you your yours yourself you'll you're you've you'd kalillac
    remember remembers remembered retain retains retained forget forgets forgot
""".split())
PROGRAMMING_OBJECTS = frozenset("""
    directory folder variable function array loop pointer index iteration cursor thread branch commit
""".split())
FRESH_WORDS = frozenset("today tonight tomorrow current currently latest newest recent upcoming breaking".split())
# Present-relative periods: "this" + a period, plus a few fixed phrases.
# "during the 2020 season" has no "this", so it is never one of them.
PRESENT_PERIODS = frozenset("week weekend morning afternoon evening month quarter season year".split())
FRESH_PHRASES = (
    ("right", "now"), ("at", "the", "moment"), ("as", "of", "now"), ("so", "far"), ("most", "recent"),
    ("up", "to", "date"), ("year", "to", "date"),
)
EVENT_NOUNS = frozenset("""
    game games match matches election elections launch release event events race fight episode season
    eclipse flight concert tour meeting holiday
""".split())
LIVE_NOUNS = frozenset("score scores status results updates coverage".split())
CHANGING_CATEGORIES = frozenset("price prices weather forecast standings headlines outage".split())
CHANGING_PHRASES = (("stock", "price"), ("share", "price"), ("exchange", "rate"))
# "score" alone is not changing data (a musical score, a test score); it is
# when the message also carries sports or event terminology.
SCORE_WORDS = frozenset(("score", "scores"))
SPORTS_CONTEXT = frozenset("""
    game games match matches team teams league playoff playoffs final finals tournament cup inning innings
    halftime overtime nba nfl mlb nhl wnba mls ncaa
""".split())
# Words that only express freshness: never the subject.
_NOT_SUBJECT = FRESH_WORDS | PRESENT_PERIODS | frozenset("now right moment next live tonight date far "
                                                         "this".split())


def _has_phrase(tokens: list[str], phrases) -> bool:
    for phrase in phrases:
        size = len(phrase)
        if any(tuple(tokens[i:i + size]) == phrase for i in range(len(tokens) - size + 1)):
            return True
    return False


def _explicit_past(text: str, today: date) -> bool:
    """An explicit date before today, or a year before this year."""
    for match in _DATE_MDY.finditer(text):
        if _before(today, match.group(3), _MONTH_NUMBER[match.group(1)], match.group(2)):
            return True
    for match in _DATE_DMY.finditer(text):
        if _before(today, match.group(3), _MONTH_NUMBER[match.group(2)], match.group(1)):
            return True
    for match in _DATE_ISO.finditer(text):
        if _before(today, match.group(1), match.group(2), match.group(3)):
            return True
    return any(int(match.group(1)) < today.year for match in _YEAR.finditer(text))


def _before(today: date, year, month, day) -> bool:
    try:
        return date(int(year), int(month), int(day)) < today
    except ValueError:
        return False


def _fresh(tokens: list[str], text: str, today: date) -> bool:
    if any(token in FRESH_WORDS for token in tokens) or _has_phrase(tokens, FRESH_PHRASES):
        return True
    for i, token in enumerate(tokens):
        if token == "this" and i + 1 < len(tokens) and tokens[i + 1] in PRESENT_PERIODS:
            return True                         # this season, this month, this year, ...
        if token == "next" and any(t in EVENT_NOUNS for t in tokens[i + 1:i + 4]):
            return True
        if token == "live" and i + 1 < len(tokens) and tokens[i + 1] in LIVE_NOUNS:
            return True
    # Changing-data categories describe the present only when nothing places
    # the question in the past: a past-tense opening, "history", or an
    # explicit past date or year ("the 1990 World Cup final", "on July 14,
    # 1980"). Explicit freshness above always wins ("the latest update for
    # Windows Server 2022").
    if _PAST.match(text) or "history" in tokens or "historical" in tokens or _explicit_past(text, today):
        return False
    sports_score = (any(token in SCORE_WORDS for token in tokens)
                    and any(token in SPORTS_CONTEXT for token in tokens))
    return (sports_score or any(token in CHANGING_CATEGORIES for token in tokens)
            or _has_phrase(tokens, CHANGING_PHRASES) or bool(_STATUS.match(text)))


def live_information(message: str, today: date | None = None) -> str | None:
    """"subject" when the message is a live-information request with a
    subject, "pronoun" when its only referent is a pronoun, else None.
    `today` (UTC by default) only dates explicit past dates and years."""
    raw = str(message)
    if len(raw) > USER_MESSAGE_MAX or _CODE.search(raw.casefold()):
        return None
    today = today or datetime.now(timezone.utc).date()
    unquoted = _QUOTED.sub(" ", raw)
    full, evidence = _analysis(unquoted)
    text = _POLITE.sub("", full)
    spans = _token_spans(text, evidence[len(full) - len(text):])
    tokens = [token for token, _ in spans]
    if not tokens or _WRITING.match(text) or _CONCEPTUAL.match(text):
        return None
    if _is_language_question(_language_copy(raw)):
        return None                             # the whole request is a question about a word
    if any(token in PROGRAMMING_OBJECTS for token in tokens):
        return None
    request = _REQUEST.match(text)
    if not (_INTERROGATIVE.match(text) or request or text.rstrip().endswith("?")):
        return None
    # The underlying question, after any request prefix: a reference to the
    # assistant or its memory anywhere in it makes the question about
    # Kalillac ("Can you tell me whether you will remember this tomorrow?").
    underlying = text[request.end():] if request else text
    if any(token in SELF_REFERENCE for token in _tokens(underlying)):
        return None
    if not _fresh(tokens, text, today):
        return None
    subject = []
    for token, evidenced in spans:
        if token in PRONOUNS:
            break
        if (evidenced and token not in FUNCTION_WORDS and token not in _NOT_SUBJECT
                and not _is_attribute(token) and not token.isdigit()):
            subject.append(token)
    if subject:
        return "subject"
    return "pronoun" if any(token in PRONOUNS for token in tokens) else None
