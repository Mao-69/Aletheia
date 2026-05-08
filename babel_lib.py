"""Library of Aletheia — Babel-style bijection.

A faithful Python port of Tom Snelling's `babel` (TypeScript). Every page in
the library has a unique identifier `ROOM.WALL.SHELF.BOOK.PAGE`; every
identifier maps to exactly one page of content, and every page of content maps
to exactly one identifier.

Math
----
The trick is a modular bijection. Let

    ALPHA       = the 42-character content alphabet
    BOOK_LENGTH = 1,312,000 chars per book (40 lines × 80 chars × 410 pages)
    N           = |ALPHA|^BOOK_LENGTH      (the entire content space)
    C           = a random integer with gcd(C, N) = 1
    I           = C^-1 mod N

Then for any `bookIndex` in [0, N):

    bookContents = (bookIndex * C) mod N         # generate
    bookIndex    = (bookContents * I) mod N      # search

Because `C` is invertible mod `N`, this is a bijection: distinct indices yield
distinct contents and vice versa. Source of the trick:
https://ericlippert.com/2013/11/14/a-practical-use-of-multiplicative-inverses

Why gmpy2
---------
N is a ~7-million-bit integer. Pure-Python `int` can do everything we need, but
a single big × big mod N takes ~80 seconds. gmpy2 (Python wrapper for GMP)
brings that to ~100ms. Required.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable

import gmpy2
from gmpy2 import mpz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 42-character content alphabet: lowercase Latin, six symbols, digits.
# Order is meaningful — index in this string is the character's "value".
ALPHA: Final[str] = "abcdefghijklmnopqrstuvwxyz,.!?- 0123456789"
ALPHA_LEN: Final[int] = len(ALPHA)
assert ALPHA_LEN == 42

# Standard alphanumeric base-32 alphabet (used by gmpy2.digits(32) and friends).
# This is the alphabet for ROOM identifiers — keeps URLs short and ASCII-safe.
ROOM_ALPHA: Final[str] = "0123456789abcdefghijklmnopqrstuv"

# gmpy2.digits(42) uses '0-9A-Za-f' (digits, then UPPERCASE letters, then lowercase a-f).
# Verify with: [gmpy2.mpz(i).digits(42) for i in range(42)]
_GMPY2_BASE_ALPHA: Final[str] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
assert len(_GMPY2_BASE_ALPHA) == 42

# Translation tables:
#   gmpy2 digit char -> ALPHA char (used after gmpy2 stringifies an mpz)
#   ALPHA char -> gmpy2 digit char (used before parsing back into mpz)
# str.maketrans + str.translate is the fastest path for char-by-char remapping
# of multi-megabyte strings — implemented in C.
_GMPY2_TO_ALPHA: Final = str.maketrans(_GMPY2_BASE_ALPHA, ALPHA)
_ALPHA_TO_GMPY2: Final = str.maketrans(ALPHA, _GMPY2_BASE_ALPHA)

# Set of valid content characters, for filtering search input.
ALPHA_SET: Final[frozenset[str]] = frozenset(ALPHA)

# Library geometry — matches babel-master.
WALLS: Final[int] = 4
SHELVES: Final[int] = 5
BOOKS: Final[int] = 32
PAGES: Final[int] = 410
LINES: Final[int] = 40
CHARS: Final[int] = 80
PAGE_LENGTH: Final[int] = LINES * CHARS                # 3,200
BOOK_LENGTH: Final[int] = PAGE_LENGTH * PAGES          # 1,312,000
BOOKS_PER_ROOM: Final[int] = WALLS * SHELVES * BOOKS   # 640


# ---------------------------------------------------------------------------
# Constants generation / persistence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BabelConstants:
    N: object  # mpz; Python's typing doesn't model gmpy2 types well
    C: object
    I: object


def _generate_constants() -> BabelConstants:
    """Pick fresh N, C, I. C is uniformly random in [1, N) and coprime with N.

    N = |ALPHA|^BOOK_LENGTH = 42^1,312,000 has prime factors {2, 3, 7}, so
    gcd(C, N) = 1 iff gcd(C, 42) = 1 — a much cheaper check than gcd against
    the full N. P(random ≡ coprime) = φ(42)/42 = 12/42 ≈ 28.6%, so the
    rejection loop terminates after ~3 attempts on average.
    """
    N = mpz(ALPHA_LEN) ** BOOK_LENGTH
    n_bytes = (N.bit_length() + 7) // 8

    while True:
        # secrets.token_bytes is CSPRNG-backed; we want unpredictable C so
        # the bijection isn't trivially reverse-engineerable.
        C = mpz(int.from_bytes(secrets.token_bytes(n_bytes), "big")) % N
        if C < 2:
            continue
        if gmpy2.gcd(C, mpz(42)) == 1:
            break

    I = gmpy2.invert(C, N)
    # Sanity check — invariant is essential for correctness.
    assert (C * I) % N == 1
    return BabelConstants(N=N, C=C, I=I)


def load_or_create_constants(path: Path) -> BabelConstants:
    """Load cached C and I from disk; generate them if missing.

    Storage is gmpy2's binary format — much smaller and faster to read than
    the multi-megabyte text representation in babel-master's `numbers` file.
    N is recomputed each startup (instant: ~15ms with gmpy2).
    """
    N = mpz(ALPHA_LEN) ** BOOK_LENGTH

    if path.exists():
        try:
            data = path.read_bytes()
            # Two big-endian length-prefixed records: [4-byte len][C bytes][4-byte len][I bytes]
            offset = 0
            c_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            C_bytes = data[offset:offset + c_len]
            offset += c_len
            i_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            I_bytes = data[offset:offset + i_len]

            C = gmpy2.from_binary(C_bytes)
            I = gmpy2.from_binary(I_bytes)

            # Verify integrity — guards against a corrupt file silently
            # producing wrong content/identifier mappings forever after.
            if (C * I) % N != 1:
                raise ValueError("Cached C and I are not modular inverses")
            return BabelConstants(N=N, C=C, I=I)
        except Exception:
            # Fall through to regeneration; the cache file is broken.
            pass

    consts = _generate_constants()
    C_bytes = gmpy2.to_binary(consts.C)
    I_bytes = gmpy2.to_binary(consts.I)
    payload = (
        len(C_bytes).to_bytes(4, "big") + C_bytes
        + len(I_bytes).to_bytes(4, "big") + I_bytes
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic write: write to temp, then rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
    return consts


# ---------------------------------------------------------------------------
# Content <-> integer conversion
# ---------------------------------------------------------------------------

def content_to_int(content: str) -> object:
    """Map a BOOK_LENGTH-character string in ALPHA to an integer in [0, N).

    Pads / trims `content` to exactly BOOK_LENGTH characters with spaces. Any
    character outside ALPHA is replaced with a space (the canonical filler).
    """
    if len(content) > BOOK_LENGTH:
        content = content[:BOOK_LENGTH]
    elif len(content) < BOOK_LENGTH:
        content = content + " " * (BOOK_LENGTH - len(content))

    # Replace anything outside ALPHA with space. Building a sanitized copy is
    # cheap; doing it as one .translate over a precomputed table is fastest.
    # Build the translation map lazily so the cost is paid once per process.
    sanitized = _sanitize_to_alpha(content)

    # Now translate each ALPHA character to its gmpy2 digit equivalent and
    # ask gmpy2 to parse the whole thing as a base-42 integer.
    digits = sanitized.translate(_ALPHA_TO_GMPY2)
    return mpz(digits, ALPHA_LEN)


def int_to_content(n: object) -> str:
    """Inverse of content_to_int: render `n` as a BOOK_LENGTH-character string in ALPHA."""
    digits = mpz(n).digits(ALPHA_LEN)
    if len(digits) < BOOK_LENGTH:
        digits = "0" * (BOOK_LENGTH - len(digits)) + digits
    return digits.translate(_GMPY2_TO_ALPHA)


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

# Pre-build a translation table: every codepoint NOT in ALPHA maps to space.
# This works at the bytes level for ASCII, and falls back to Python-level
# replacement for non-ASCII (which we sanitize with str.translate using a dict).
_ALPHA_BYTES = ALPHA.encode("ascii")


def _sanitize_to_alpha(s: str) -> str:
    """Replace every non-ALPHA character with space.

    Lowercases letters first (so 'Hello' -> 'hello'). Newlines and tabs become
    spaces. Anything else not in ALPHA — accents, emoji, punctuation we don't
    support — also becomes a space. The output is guaranteed to be in ALPHA.
    """
    s = s.lower()
    # Build the result as a list once; faster than repeated string concatenation.
    out: list[str] = []
    for ch in s:
        out.append(ch if ch in ALPHA_SET else " ")
    return "".join(out)


def sanitize_for_search(content: str) -> str:
    """Public sanitizer — normalises whitespace and strips disallowed characters."""
    # Collapse \r\n and \r to \n, then map newlines (and any non-ALPHA) to space.
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return _sanitize_to_alpha(normalized)


# ---------------------------------------------------------------------------
# Identifier (ROOM.WALL.SHELF.BOOK.PAGE) <-> sequential book index
# ---------------------------------------------------------------------------
#
# A "sequential book index" is a number in [1, N / BOOKS_PER_ROOM × 640]
# that uniquely identifies one of the books in the library, ignoring its
# page subdivision. Conversion to/from identifier is purely arithmetic.
# ---------------------------------------------------------------------------

class BabelIdentifierError(ValueError):
    """Raised for malformed or out-of-range identifiers."""


def parse_identifier(identifier: str) -> tuple[str, int, int, int, int]:
    """Split an identifier into (room, wall, shelf, book, page) with bounds-checking."""
    parts = identifier.split(".")
    if len(parts) != 5:
        raise BabelIdentifierError("Identifier must have form ROOM.WALL.SHELF.BOOK.PAGE")
    room_s, wall_s, shelf_s, book_s, page_s = parts
    if not room_s:
        raise BabelIdentifierError("Room cannot be empty")
    # Room must use the base-32 alphabet only.
    if any(c not in ROOM_ALPHA for c in room_s):
        raise BabelIdentifierError("Room contains characters outside the base-32 alphabet")

    try:
        wall = int(wall_s)
        shelf = int(shelf_s)
        book = int(book_s)
        page = int(page_s)
    except ValueError as e:
        raise BabelIdentifierError("Wall, shelf, book, page must be integers") from e

    if not 1 <= wall <= WALLS:
        raise BabelIdentifierError(f"Wall must be in [1, {WALLS}]; got {wall}")
    if not 1 <= shelf <= SHELVES:
        raise BabelIdentifierError(f"Shelf must be in [1, {SHELVES}]; got {shelf}")
    if not 1 <= book <= BOOKS:
        raise BabelIdentifierError(f"Book must be in [1, {BOOKS}]; got {book}")
    if not 1 <= page <= PAGES:
        raise BabelIdentifierError(f"Page must be in [1, {PAGES}]; got {page}")

    return room_s, wall, shelf, book, page


def identifier_to_seqnum(identifier: str, consts: BabelConstants) -> tuple[object, int]:
    """Identifier -> (sequential book index, page number)."""
    room_s, wall, shelf, book, page = parse_identifier(identifier)

    # Strip a single leading zero string only — '0' itself is invalid (room must be >= 1)
    # but '01' should normalise to '1'. Keep it simple: int parses both fine.
    int_room = mpz(room_s, 32)
    if int_room < 1:
        raise BabelIdentifierError("Room cannot be smaller than 1")

    # Total books in the library = N / BOOK_LENGTH? No — every integer in [0, N)
    # is one valid BOOK CONTENT, but books are organised in groups of BOOKS_PER_ROOM
    # per room. Total rooms = N / BOOKS_PER_ROOM (exact since N is much bigger).
    total_rooms = consts.N // BOOKS_PER_ROOM
    if int_room > total_rooms:
        raise BabelIdentifierError("Room is too large for this library")

    # seq = (room - 1) * BOOKS_PER_ROOM + (wall - 1) * SHELVES * BOOKS
    #     + (shelf - 1) * BOOKS + book
    seq = (int_room - 1) * BOOKS_PER_ROOM
    seq += (wall - 1) * SHELVES * BOOKS
    seq += (shelf - 1) * BOOKS
    seq += book
    return seq, page


def seqnum_to_identifier(seq: object, page: int, consts: BabelConstants) -> str:
    """Sequential book index -> 'room.wall.shelf.book.page' identifier."""
    seq = mpz(seq) - 1  # algorithm is 0-indexed internally; identifiers are 1-indexed
    if seq < 0:
        return f"1.1.1.1.{page}"
    if seq >= consts.N:
        # Shouldn't happen; total seq range is N // BOOKS_PER_ROOM × BOOKS_PER_ROOM <= N.
        raise BabelIdentifierError("Sequential index outside library")

    room_idx, rem = gmpy2.f_divmod(seq, BOOKS_PER_ROOM)
    room_idx = room_idx + 1
    wall_idx, rem = gmpy2.f_divmod(rem, SHELVES * BOOKS)
    wall_idx = int(wall_idx) + 1
    shelf_idx, book_idx = gmpy2.f_divmod(rem, BOOKS)
    shelf_idx = int(shelf_idx) + 1
    book_idx = int(book_idx) + 1

    room_str = mpz(room_idx).digits(32)
    return f"{room_str}.{wall_idx}.{shelf_idx}.{book_idx}.{page}"


# ---------------------------------------------------------------------------
# The main operations: generate, lookup, random
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedPage:
    identifier: str
    content: str            # exactly PAGE_LENGTH chars (3,200)
    room: str
    wall: int
    shelf: int
    book: int
    page: int
    prev_identifier: str
    next_identifier: str


def generate_content(identifier: str, consts: BabelConstants) -> GeneratedPage:
    """Render the page at the given identifier.

    This is the hot path for Browse: identifier -> sequential index ->
    multiply by C mod N -> base-42 string -> slice out the requested page.
    """
    seq, page = identifier_to_seqnum(identifier, consts)
    book_value = (seq * consts.C) % consts.N
    full_book = int_to_content(book_value)

    start = (page - 1) * PAGE_LENGTH
    page_text = full_book[start:start + PAGE_LENGTH]

    room_str, wall, shelf, book, _ = parse_identifier(identifier)

    # Compute prev and next page identifiers. Crossing book boundaries means
    # incrementing/decrementing the sequential index and wrapping the page.
    if page == PAGES:
        next_id = seqnum_to_identifier(seq + 1, 1, consts)
    else:
        next_id = seqnum_to_identifier(seq, page + 1, consts)

    if page == 1:
        prev_id = seqnum_to_identifier(seq - 1, PAGES, consts)
    else:
        prev_id = seqnum_to_identifier(seq, page - 1, consts)

    return GeneratedPage(
        identifier=identifier,
        content=page_text,
        room=room_str,
        wall=wall,
        shelf=shelf,
        book=book,
        page=page,
        prev_identifier=prev_id,
        next_identifier=next_id,
    )


def lookup_content(content: str, consts: BabelConstants, page: int = 1) -> str:
    """Inverse of generate_content: find the identifier whose page contains `content`.

    `content` should already be padded to BOOK_LENGTH (use the search-mode helpers
    in `search.py`). `page` is the page number within the resulting book.
    """
    if not 1 <= page <= PAGES:
        raise BabelIdentifierError(f"Page must be in [1, {PAGES}]; got {page}")
    book_value = content_to_int(content)
    seq = (book_value * consts.I) % consts.N
    return seqnum_to_identifier(seq, page, consts)


def random_identifier(consts: BabelConstants) -> str:
    """A uniformly random identifier in the library."""
    # Random sequential index in [1, total_books]
    total_books = consts.N // BOOK_LENGTH * (PAGES * 1)  # not used; we use seqnum range
    # Total *books* (not pages): rooms × BOOKS_PER_ROOM = (N / BOOKS_PER_ROOM) × BOOKS_PER_ROOM = N
    # so any seq in [1, N] is fine — but we're indexing books, so cap is N.
    seq_range = consts.N
    # Use secrets for unpredictability.
    n_bytes = (int(seq_range).bit_length() + 7) // 8
    while True:
        candidate = mpz(int.from_bytes(secrets.token_bytes(n_bytes), "big"))
        if 1 <= candidate <= seq_range:
            break
    page = secrets.randbelow(PAGES) + 1
    return seqnum_to_identifier(candidate, page, consts)
