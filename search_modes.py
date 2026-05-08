"""Search modes — fill the rest of a 1.3M-char book around the user's content.

Three modes, ported from babel-master:

  empty   The user's content sits at the start of one randomly-chosen page;
          the rest of the book is spaces. Most "natural" — clean white pages
          either side of your phrase.

  chars   The user's content is embedded at a random offset, surrounded by
          purely random characters from ALPHA. Maximally Borgesian — the kind
          of book you'd find by accident.

  words   Like 'chars' but filler is built from common English words instead
          of pure noise — gives the eerie sensation of finding your phrase in
          something that looks like a book.

Each mode returns:

    (book_text, page_number, highlight)

where book_text is exactly BOOK_LENGTH characters, page_number is 1..PAGES,
and highlight is None or (start_line, start_col, end_line, end_col) for the UI
to draw a box around the matched span.
"""

from __future__ import annotations

import random
from typing import NamedTuple

from babel_lib import (
    ALPHA,
    BOOK_LENGTH,
    CHARS,
    LINES,
    PAGES,
    PAGE_LENGTH,
    sanitize_for_search,
)

# A small but distinctive set of common English words. Loaded from the words
# file at module import (cheap), filtered to ALPHA chars, sorted by length so
# the search-mode filler can grab "fits in N chars" candidates quickly.
_COMMON_WORDS: list[str] = []


def _load_common_words() -> list[str]:
    """The 1,000 or so most-common English words, filtered to ALPHA letters only.

    Sourced from a small embedded list rather than a large file dependency.
    The exact corpus doesn't matter — they just need to look like words.
    """
    if _COMMON_WORDS:
        return _COMMON_WORDS

    # The 1000 most common English words. Embedded so we have no extra files
    # to ship. List sourced from public-domain word-frequency datasets.
    words_text = (
        "the of and to a in for is on that by this with i you it not or be are from "
        "at as your all have new more an was we will home can us about if page my has "
        "search free but our one other do no information time they site he up may what "
        "which their news out use any there see only so his when contact here business "
        "who web also now help get pm view online first am been would how were me some "
        "these click its like service than find price date back top people had list "
        "name just over state year day into email two health world re next used go "
        "work last most products music buy data make them should product system post "
        "her city add policy number such please available copyright support message "
        "after best software then jan good video well where info rights public books "
        "high school through each links she review years order very privacy book items "
        "company read group sex need many user said de does set under general research "
        "university january mail full map reviews program life know games way days "
        "management part could great united hotel real item international center ebay "
        "must store travel comments made development report off member details line "
        "terms before hotels did send right type because local those using results "
        "office education national car design take posted internet address community "
        "within states area want phone shipping reserved subject between forum family "
        "long based code show even black check special prices website index being "
        "women much sign file link open today technology south case project same "
        "pages uk version section own found sports house related security both "
        "county american game members power while care network down computer systems "
        "three total place end following download him without per access think north "
        "resources current posts big media law control water history pictures size "
        "art personal since including guide shop directory board location change "
        "white text small rating rate government children during usa return students "
        "shopping account times sites level digital profile previous form events love "
        "old john main call hours image department title description non insurance "
        "another why shall property class still money quality every listing content "
        "country private little visit save tools low reply customer december compare "
        "movies include college value article york man card jobs provide food source "
        "author different press learn sale around print course job canada process "
        "teen room stock training too credit point join science men categories "
        "advanced west sales look english left team estate box conditions select "
        "windows photos gay thread week category note live large gallery table "
        "register however june october november market library really action start "
        "series model features air industry plan human provided tv yes required "
        "second hot accessories cost movie forums march la september better say "
        "questions july yahoo going medical test friend come dec server study "
        "application cart staff articles feedback again play looking issues april "
        "never users complete street topic comment financial things working against "
        "standard tax person below mobile less got blog party payment equipment "
        "login student let programs offers legal above recent park stores side act "
        "problem red give memory performance social august quote language story "
        "sell experience rates create key body young america important field few "
        "east paper single ii age activities club example girls additional password "
        "latest something road gift question changes night ca hard texas oct pay "
        "four poker status browse issue range building seller court february always "
        "result audio light write war nov offer blue groups easy given files event "
        "release analysis request china making picture needs possible might "
        "professional yet month major star areas future space committee hand sun "
        "cards problems london washington meeting rss become interest id child "
        "keep enter california porn share similar garden schools million added "
        "reference companies listed baby learning energy run delivery net popular "
        "term film stories put computers journal reports welcome solutions voice "
        "friends schedule purchase materials wood mr level feel head premium "
        "packages turn played primary cause picks practice quickly brand removed "
        "useful housing roman policies section apartments however actions wanted "
        "monster blogs golden residents fingers liability sleep components labour "
        "mexican households scotland session standards ban remind seemed extension "
        "candy plot folder eight indian craft eating canyon mistake natural agency "
        "steel urban afford recover champion suzuki greatest rates manga affect"
    )
    cleaned: list[str] = []
    seen: set[str] = set()
    for word in words_text.split():
        word = "".join(c for c in word.lower() if c in ALPHA and c != " ")
        if word and len(word) >= 2 and word not in seen:
            cleaned.append(word)
            seen.add(word)
    cleaned.sort(key=len)
    _COMMON_WORDS.extend(cleaned)
    return _COMMON_WORDS


class Highlight(NamedTuple):
    """Coordinates for the matched span on the book page."""
    start_line: int
    start_col: int
    end_line: int
    end_col: int


class SearchResult(NamedTuple):
    book: str                # exactly BOOK_LENGTH chars
    page: int                # which page (1..PAGES) contains the user's content
    highlight: Highlight | None


def _highlight_from_offset(start: int, length: int) -> Highlight:
    """Convert (char-offset-in-book, length) to (start_line, col, end_line, col).

    Lines and columns are zero-indexed within the page; the caller adjusts
    for which page the offset lands on.
    """
    start_line = start // CHARS
    start_col = start % CHARS
    end_pos = start + length
    end_line = end_pos // CHARS
    end_col = end_pos % CHARS
    return Highlight(start_line, start_col, end_line, end_col)


def _random_alpha_chars(n: int, rng: random.Random) -> str:
    """Generate `n` characters chosen uniformly at random from ALPHA."""
    # Generating one character at a time with rng.choice would be slow for
    # 1.3M chars; rng.choices does it in C-implemented batch.
    return "".join(rng.choices(ALPHA, k=n))


def search_empty(content: str, *, rng: random.Random | None = None) -> SearchResult:
    """The user's content occupies the first lines of one random page; rest is spaces."""
    rng = rng or random.Random()
    content = sanitize_for_search(content)

    chosen_page = rng.randrange(PAGES)
    start_in_book = chosen_page * PAGE_LENGTH

    # Lay out the content within the chosen page only — line-wrapped on \n,
    # padded to CHARS-wide rows. Anything that overflows the page is dropped.
    page_buf = [" "] * PAGE_LENGTH
    cursor = 0
    for line in content.split("\n"):
        if cursor >= PAGE_LENGTH:
            break
        # Place this line starting at the next CHARS-aligned position.
        # (Mirrors babel-master's line-aligned layout.)
        line = line[: PAGE_LENGTH - cursor]
        for i, ch in enumerate(line):
            page_buf[cursor + i] = ch
        # Advance to next CHARS-aligned position.
        consumed = len(line)
        cursor += consumed
        # Pad current line to the next 80-char boundary.
        if cursor < PAGE_LENGTH and cursor % CHARS != 0:
            cursor += CHARS - (cursor % CHARS)

    # Build the full book: spaces everywhere except this page.
    book = (" " * start_in_book) + "".join(page_buf) + (" " * (BOOK_LENGTH - start_in_book - PAGE_LENGTH))
    return SearchResult(book=book, page=chosen_page + 1, highlight=None)


def search_chars(content: str, *, rng: random.Random | None = None) -> SearchResult:
    """Content embedded at a random offset within fully random ALPHA noise."""
    rng = rng or random.Random()
    content = sanitize_for_search(content).replace("\n", "")
    if len(content) > BOOK_LENGTH:
        content = content[:BOOK_LENGTH]

    book_chars = list(_random_alpha_chars(BOOK_LENGTH, rng))
    max_start = BOOK_LENGTH - len(content)
    start = rng.randint(0, max(0, max_start))
    for i, ch in enumerate(content):
        book_chars[start + i] = ch
    book = "".join(book_chars)

    highlight = _highlight_from_offset(start, len(content))
    page = (start // PAGE_LENGTH) + 1
    # Translate highlight coords into the chosen page's coordinate space.
    page_offset_lines = (page - 1) * LINES
    highlight = Highlight(
        start_line=highlight.start_line - page_offset_lines,
        start_col=highlight.start_col,
        end_line=highlight.end_line - page_offset_lines,
        end_col=highlight.end_col,
    )
    return SearchResult(book=book, page=page, highlight=highlight)


def search_words(content: str, *, rng: random.Random | None = None) -> SearchResult:
    """Content embedded in plausibly-readable text (common-word filler)."""
    rng = rng or random.Random()
    content = sanitize_for_search(content).replace("\n", "")
    if len(content) > BOOK_LENGTH:
        content = content[:BOOK_LENGTH]

    words = _load_common_words()
    # The word list is sorted by length; bisect lets us find "words of length <= N"
    # in O(log n) per pick instead of O(n) by scanning the whole list every time.
    # That difference matters: BOOK_LENGTH / avg-word-length ≈ 250k picks per search.
    import bisect
    word_lengths = [len(w) for w in words]
    rng_choice = rng.choice
    rng_randrange = rng.randrange

    def pick_word_fitting(budget: int) -> str | None:
        """Return a random word whose length <= budget, or None if no such word."""
        if budget < 2:  # all words have len >= 2
            return None
        cutoff = bisect.bisect_right(word_lengths, budget)
        if cutoff == 0:
            return None
        return words[rng_randrange(cutoff)]

    max_start = BOOK_LENGTH - len(content)
    start = rng.randint(0, max(0, max_start))

    parts: list[str] = []
    used_chars = 0
    while used_chars < start:
        budget = start - used_chars - 1  # leave room for the trailing space
        if budget <= 0:
            break
        word = pick_word_fitting(budget)
        if word is None:
            parts.append(" " * (start - used_chars))
            used_chars = start
            break
        parts.append(word)
        parts.append(" ")
        used_chars += len(word) + 1

    prefix = "".join(parts)
    if len(prefix) < start:
        prefix += " " * (start - len(prefix))
    elif len(prefix) > start:
        prefix = prefix[:start]

    suffix_parts: list[str] = []
    suffix_target = BOOK_LENGTH - start - len(content)
    used = 0
    while used < suffix_target:
        budget = suffix_target - used - 1
        if budget <= 0:
            break
        word = pick_word_fitting(budget)
        if word is None:
            suffix_parts.append(" " * (suffix_target - used))
            used = suffix_target
            break
        suffix_parts.append(word)
        suffix_parts.append(" ")
        used += len(word) + 1

    suffix = "".join(suffix_parts)
    if len(suffix) < suffix_target:
        suffix += " " * (suffix_target - len(suffix))
    elif len(suffix) > suffix_target:
        suffix = suffix[:suffix_target]

    book = prefix + content + suffix
    assert len(book) == BOOK_LENGTH

    highlight = _highlight_from_offset(start, len(content))
    page = (start // PAGE_LENGTH) + 1
    page_offset_lines = (page - 1) * LINES
    highlight = Highlight(
        start_line=highlight.start_line - page_offset_lines,
        start_col=highlight.start_col,
        end_line=highlight.end_line - page_offset_lines,
        end_col=highlight.end_col,
    )
    return SearchResult(book=book, page=page, highlight=highlight)


def search(content: str, mode: str = "empty", *, rng: random.Random | None = None) -> SearchResult:
    """Dispatch to the right search-mode implementation."""
    if mode == "empty":
        return search_empty(content, rng=rng)
    if mode == "chars":
        return search_chars(content, rng=rng)
    if mode == "words":
        return search_words(content, rng=rng)
    raise ValueError(f"Unknown search mode: {mode!r}")
