"""Test suite for the Babel-style Aletheia.

Three layers:

  * Math (`babel_lib.py`): the bijection. Every test here verifies the
    invariant `lookup(generate(x)) == x`.
  * Search modes (`search_modes.py`): each mode produces a BOOK_LENGTH
    string that, when fed through lookup, returns an identifier whose page
    contains the original text.
  * HTTP (`app.py`): endpoints round-trip through the bookmark store and
    return well-formed JSON.

The test fixtures share a single set of math constants — generating them
takes ~1.5s, and the bijection is independent of the cache file.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Ensure the project directory is importable when pytest is run from elsewhere.
sys.path.insert(0, str(Path(__file__).parent))

import babel_lib as B  # noqa: E402
import search_modes as S  # noqa: E402
from bookmark_store import (  # noqa: E402
    BookmarkStore,
    is_hashed_identifier,
    split_hashed_identifier,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def consts(tmp_path_factory) -> B.BabelConstants:
    """Generate (or reuse) math constants once for the whole session."""
    cache = tmp_path_factory.mktemp("babel-numbers") / "numbers.bin"
    return B.load_or_create_constants(cache)


@pytest.fixture
def bookmarks(tmp_path: Path) -> BookmarkStore:
    return BookmarkStore(tmp_path / "bookmarks.sqlite3")


@pytest.fixture
def client(tmp_path_factory, monkeypatch) -> TestClient:
    """An isolated TestClient with its own bookmark DB and reused math constants.

    We swap `BASE_DIR`-derived paths to a per-test temp directory so the test
    suite never touches a developer's real numbers.bin or bookmarks.sqlite3.
    """
    workdir = tmp_path_factory.mktemp("aletheia-app")

    # Reuse the session-scoped numbers cache by symlinking, so tests don't pay
    # the ~1.5s constants-gen cost again.
    session_consts = B.load_or_create_constants(workdir / "numbers.bin")
    assert session_consts.N.bit_length() > 0  # sanity

    # Patch the module-level paths in app.py to point at workdir.
    import app as app_module  # noqa: WPS433
    monkeypatch.setattr(app_module, "BASE_DIR", workdir)
    monkeypatch.setattr(app_module, "STATIC_DIR", workdir / "static")
    monkeypatch.setattr(app_module, "NUMBERS_BIN", workdir / "numbers.bin")
    monkeypatch.setattr(app_module, "BOOKMARK_DB", workdir / "bookmarks.sqlite3")
    monkeypatch.setattr(app_module, "BUNNY_IMAGE", workdir / "static" / "cyberpunk_bunny.png")

    # Static dir must exist for StaticFiles to mount; copy assets we need.
    (workdir / "static").mkdir(exist_ok=True)
    real_static = Path(__file__).parent / "static"
    for name in ("index.html", "styles.css", "app.js", "cyberpunk_bunny.png", "favicon.ico"):
        src = real_static / name
        if src.exists():
            (workdir / "static" / name).write_bytes(src.read_bytes())

    # The TestClient runs the lifespan; it'll find numbers.bin already cached.
    with TestClient(app_module.app) as c:
        yield c


# ---------------------------------------------------------------------------
# Math: bijection round-trips
# ---------------------------------------------------------------------------

class TestBijection:
    def test_constants_are_inverse(self, consts: B.BabelConstants):
        """C × I ≡ 1 (mod N) is the bedrock invariant. If this is wrong, nothing else works."""
        assert (consts.C * consts.I) % consts.N == 1

    def test_int_to_content_round_trip(self, consts: B.BabelConstants):
        """A random integer, stringified, parsed back, equals itself."""
        # Pick a few magnitudes — small, medium, near-N.
        for n in [B.mpz(0), B.mpz(1), B.mpz(42), B.mpz(42)**100, consts.N - 1]:
            text = B.int_to_content(n)
            assert len(text) == B.BOOK_LENGTH
            assert all(c in B.ALPHA for c in text[-100:])  # last 100 chars are ALPHA
            n2 = B.content_to_int(text)
            assert n == n2

    def test_identifier_round_trip(self, consts: B.BabelConstants):
        """seqnum -> identifier -> seqnum is identity."""
        # Test seqnums spanning a few orders of magnitude.
        for seq in [B.mpz(1), B.mpz(640), B.mpz(641), B.mpz(10**20)]:
            ident = B.seqnum_to_identifier(seq, page=42, consts=consts)
            seq2, page2 = B.identifier_to_seqnum(ident, consts)
            assert seq2 == seq, f"seq={seq} → {ident} → {seq2}"
            assert page2 == 42

    def test_generate_produces_alpha_only(self, consts: B.BabelConstants):
        """Every character in generated content is in ALPHA."""
        page = B.generate_content("7g.2.3.5.42", consts)
        assert len(page.content) == B.PAGE_LENGTH
        assert set(page.content) <= set(B.ALPHA)

    def test_generate_lookup_round_trip(self, consts: B.BabelConstants):
        """generate(id).book == lookup(book) → id (when run on an identifier)."""
        ident = "7g.2.3.5.42"
        page = B.generate_content(ident, consts)
        # Reconstruct the full book from this page's content + the rest of the
        # book at the same identifier (we'd normally compute the whole book).
        # Easier check: lookup the book content for this seq directly.
        seq, _ = B.identifier_to_seqnum(ident, consts)
        full_book = B.int_to_content((seq * consts.C) % consts.N)
        ident2 = B.lookup_content(full_book, consts, page=page.page)
        assert ident2 == ident

    def test_prev_next_continuity(self, consts: B.BabelConstants):
        """next(prev(p)) == p and prev(next(p)) == p, including across book boundaries."""
        for ident in ["7g.2.3.5.42", "7g.2.3.5.1", "7g.2.3.5.410"]:
            p = B.generate_content(ident, consts)
            n = B.generate_content(p.next_identifier, consts)
            assert n.prev_identifier == ident
            pp = B.generate_content(p.prev_identifier, consts)
            assert pp.next_identifier == ident


class TestParseIdentifier:
    def test_valid(self):
        assert B.parse_identifier("7g.2.3.5.42") == ("7g", 2, 3, 5, 42)

    @pytest.mark.parametrize("bad", [
        "",
        "7g.2.3.5",            # too few parts
        "7g.2.3.5.42.99",      # too many parts
        "7g.0.3.5.42",         # wall < 1
        "7g.5.3.5.42",         # wall > WALLS
        "7g.2.6.5.42",         # shelf > SHELVES
        "7g.2.3.33.42",        # book > BOOKS
        "7g.2.3.5.411",        # page > PAGES
        "7g.2.3.5.0",          # page < 1
        "ZZZ.2.3.5.42",        # room contains non-base32
        ".2.3.5.42",           # empty room
        "7g.a.3.5.42",         # non-numeric wall
    ])
    def test_invalid(self, bad: str):
        with pytest.raises(B.BabelIdentifierError):
            B.parse_identifier(bad)


# ---------------------------------------------------------------------------
# Search modes
# ---------------------------------------------------------------------------

class TestSearchModes:
    PHRASE = "the quick brown fox jumps over the lazy dog"

    @pytest.mark.parametrize("mode", ["empty", "chars", "words"])
    def test_round_trip(self, consts: B.BabelConstants, mode: str):
        """Each mode produces a book whose returned page contains the phrase."""
        rng = random.Random(0)
        result = S.search(self.PHRASE, mode, rng=rng)
        assert len(result.book) == B.BOOK_LENGTH
        ident = B.lookup_content(result.book, consts, page=result.page)
        page = B.generate_content(ident, consts)
        assert self.PHRASE in page.content

    def test_chars_mode_has_highlight(self):
        rng = random.Random(0)
        result = S.search_chars(self.PHRASE, rng=rng)
        assert result.highlight is not None
        # Highlight bounds reasonable
        assert 0 <= result.highlight.start_line < B.LINES
        assert 0 <= result.highlight.start_col <= B.CHARS

    def test_empty_mode_no_highlight(self):
        rng = random.Random(0)
        result = S.search_empty(self.PHRASE, rng=rng)
        assert result.highlight is None

    def test_sanitization_drops_non_alpha(self, consts: B.BabelConstants):
        """Capital letters lowercase; symbols outside ALPHA become spaces."""
        rng = random.Random(0)
        result = S.search_empty("Hello, World! @ # $", rng=rng)
        ident = B.lookup_content(result.book, consts, page=result.page)
        page = B.generate_content(ident, consts)
        # `@`, `#`, `$` aren't in ALPHA — they should have become spaces
        assert "hello, world!" in page.content
        assert "@" not in page.content
        assert "#" not in page.content


# ---------------------------------------------------------------------------
# Bookmark store
# ---------------------------------------------------------------------------

class TestBookmarkStore:
    def test_remember_and_lookup(self, bookmarks: BookmarkStore):
        room = "abc123" * 100  # 600-char room
        h = bookmarks.remember(room)
        assert len(h) == 16
        assert bookmarks.lookup(h) == room

    def test_lookup_missing(self, bookmarks: BookmarkStore):
        assert bookmarks.lookup("0" * 16) is None
        assert bookmarks.lookup("") is None

    def test_remember_idempotent(self, bookmarks: BookmarkStore):
        room = "test-room"
        h1 = bookmarks.remember(room)
        h2 = bookmarks.remember(room)
        assert h1 == h2
        assert bookmarks.count() == 1

    def test_split_hashed_identifier(self):
        h, suffix = split_hashed_identifier("@abc123.1.2.3.4")
        assert h == "abc123"
        assert suffix == ".1.2.3.4"

    def test_is_hashed_identifier(self):
        assert is_hashed_identifier("@abc.1.1.1.1")
        assert not is_hashed_identifier("abc.1.1.1.1")
        assert not is_hashed_identifier("")


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

class TestHTTP:
    def test_health(self, client: TestClient):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_index_serves_html(self, client: TestClient):
        r = client.get("/")
        assert r.status_code == 200
        assert b"Library of Aletheia" in r.content

    def test_search_returns_compact(self, client: TestClient):
        r = client.post("/search", data={"content": "alpha beta", "mode": "empty"})
        assert r.status_code == 200
        body = r.json()
        assert body["compact_identifier"].startswith("@")
        # `1` (room=1) is the smallest; full identifier is much longer for short content
        assert len(body["identifier"]) > len(body["compact_identifier"])

    def test_search_invalid_mode(self, client: TestClient):
        r = client.post("/search", data={"content": "x", "mode": "nope"})
        assert r.status_code == 422

    def test_search_empty_after_sanitize(self, client: TestClient):
        # All-emoji content sanitizes to spaces, then strips to nothing.
        r = client.post("/search", data={"content": "🎉🎊🎈", "mode": "empty"})
        assert r.status_code == 422

    def test_encode_returns_qr(self, client: TestClient):
        r = client.post("/encode", data={"content": "test phrase", "mode": "empty"})
        assert r.status_code == 200
        body = r.json()
        assert body["compact_identifier"].startswith("@")
        # QR base64 should be substantial (kilobytes)
        assert len(body["qr_base64"]) > 1000
        assert body["qr_too_long"] is False

    def test_browse_compact_round_trip(self, client: TestClient):
        # Encode → browse with returned compact ID → page contains phrase
        phrase = "the library is total"
        encoded = client.post("/encode", data={"content": phrase, "mode": "empty"}).json()
        compact = encoded["compact_identifier"]
        browsed = client.post("/browse", data={"identifier": compact}).json()
        assert phrase in browsed["content"]
        assert browsed["page"] == encoded["page"]
        assert browsed["compact_identifier"] == compact

    def test_browse_unknown_hash_404(self, client: TestClient):
        # A hash this server never saw should 404 — we don't fabricate rooms.
        r = client.post("/browse", data={"identifier": "@deadbeefdeadbeef.1.1.1.1"})
        assert r.status_code == 404

    def test_browse_invalid_identifier(self, client: TestClient):
        r = client.post("/browse", data={"identifier": "garbage"})
        assert r.status_code == 422

    def test_random_returns_page(self, client: TestClient):
        r = client.post("/random")
        assert r.status_code == 200
        body = r.json()
        assert body["compact_identifier"].startswith("@")
        assert len(body["content"]) == B.PAGE_LENGTH

    def test_qr_endpoint(self, client: TestClient):
        r = client.post("/qr", data={"identifier": "@abc.1.1.1.1"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["qr_base64"]) > 1000

    def test_qr_too_long_rejected(self, client: TestClient):
        # Beyond MAX_QR_IDENTIFIER_LEN
        r = client.post("/qr", data={"identifier": "x" * 5000})
        assert r.status_code == 422

    def test_security_headers(self, client: TestClient):
        r = client.get("/")
        assert "Content-Security-Policy" in r.headers
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"

    def test_search_then_browse_full_form(self, client: TestClient):
        """Browse should also accept the full (long) identifier form, not only @hash."""
        phrase = "library bijection"
        searched = client.post("/search", data={"content": phrase, "mode": "empty"}).json()
        browsed = client.post("/browse", data={"identifier": searched["identifier"]}).json()
        assert phrase in browsed["content"]
        assert browsed["page"] == searched["page"]

    @pytest.mark.parametrize("mode", ["empty", "chars", "words"])
    def test_all_modes_via_http(self, client: TestClient, mode: str):
        phrase = "ipsum dolor sit amet"
        encoded = client.post("/encode", data={"content": phrase, "mode": mode}).json()
        browsed = client.post("/browse", data={"identifier": encoded["compact_identifier"]}).json()
        assert phrase in browsed["content"]
