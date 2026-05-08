"""Library of Aletheia — Babel-style server.

Endpoints
---------
GET  /           static SPA
GET  /health     liveness probe
POST /search     content (+mode) -> identifier
POST /encode     content (+mode) -> identifier + QR (writeable copy)
POST /browse     identifier -> page text + prev/next + room/wall/etc.
POST /random     no body -> random identifier (resolves like /browse)

Wire format
-----------
Identifiers are `ROOM.WALL.SHELF.BOOK.PAGE` strings — the same form used by
babel-master. ROOM is base-32 ([0-9a-v]); the others are decimal. There is no
checksum and no separate length: every identifier resolves to exactly one
PAGE_LENGTH (3,200) character page, and every page belongs to exactly one
identifier.

QR codes encode the identifier string directly. Identifiers from short search
inputs are short (room is the only variable-length component); for short
phrases the QR fits comfortably. For very long phrases the room can be
thousands of characters and the QR is skipped.
"""

from __future__ import annotations

import base64
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

import babel_lib as B
import search_modes as S
from bookmark_store import (
    BookmarkStore,
    is_hashed_identifier,
    split_hashed_identifier,
)
from qr_render import preload_overlay, render_qr_with_overlay

# ---------------------------------------------------------------------------
# Config (env-driven, sensible defaults)
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).resolve().parent
STATIC_DIR  = BASE_DIR / "static"
NUMBERS_BIN = BASE_DIR / "numbers.bin"
BOOKMARK_DB = BASE_DIR / "bookmarks.sqlite3"
BUNNY_IMAGE = STATIC_DIR / "cyberpunk_bunny.png"

HOST       = os.getenv("HOST", "127.0.0.1")
PORT       = int(os.getenv("PORT", "8000"))
RELOAD     = os.getenv("RELOAD", "0") == "1"
LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO")
RATE_LIMIT = os.getenv("RATE_LIMIT", "30/minute")

_origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]

# Identifier QR threshold: babel identifiers are short (a few dozen chars) for
# short input phrases. Anything past this we skip QR generation — the QR would
# fail to render anyway (max ~3000 chars).
MAX_QR_IDENTIFIER_LEN = 1500

# Input cap for /search and /encode (per babel-master, content can't exceed BOOK_LENGTH).
MAX_SEARCH_INPUT = B.BOOK_LENGTH

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("aletheia")


# ---------------------------------------------------------------------------
# Lifespan: load math constants and prewarm caches
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("Loading babel constants from %s …", NUMBERS_BIN)
    consts = B.load_or_create_constants(NUMBERS_BIN)
    app.state.babel = consts
    log.info("Constants ready: N has %s bits, file is %s bytes",
             f"{consts.N.bit_length():,}", f"{NUMBERS_BIN.stat().st_size:,}")

    app.state.bookmarks = BookmarkStore(BOOKMARK_DB)
    log.info("Bookmark store: %d known rooms in %s",
             app.state.bookmarks.count(), BOOKMARK_DB)

    if BUNNY_IMAGE.exists():
        preload_overlay(BUNNY_IMAGE)
        log.info("QR overlay preloaded")
    else:
        log.warning("QR overlay missing at %s — QRs will fail to render.", BUNNY_IMAGE)

    yield


app = FastAPI(title="Library of Aletheia", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Middleware: gzip, CORS, security headers
# ---------------------------------------------------------------------------

app.add_middleware(GZipMiddleware, minimum_size=1024)

if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        max_age=600,
    )

# Same CSP as before — Tailwind play-CDN forces 'unsafe-inline' for styles.
# Scripts are restricted to 'self'; the QR scanner library is bundled at
# /static/html5-qrcode.min.js so we don't need any third-party origin.
_CSP = "; ".join([
    "default-src 'self'",
    "img-src 'self' data: blob:",
    "media-src 'self'",
    "font-src 'self' data:",
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(self), microphone=(), geolocation=()"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=[])
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Try again later."})


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index() -> Response:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qr_for_identifier(identifier: str) -> str:
    """Render the identifier as a QR PNG, base64-encoded. Empty string if too long."""
    if len(identifier) > MAX_QR_IDENTIFIER_LEN:
        return ""
    if not BUNNY_IMAGE.exists():
        return ""
    try:
        png = render_qr_with_overlay(identifier, BUNNY_IMAGE)
        return base64.b64encode(png).decode("ascii")
    except Exception:
        log.exception("QR rendering failed for identifier of length %d", len(identifier))
        return ""


def _format_page_response(
    page: B.GeneratedPage,
    *,
    bookmarks: BookmarkStore,
    highlight: tuple[int, int, int, int] | None = None,
) -> dict:
    """Common shape returned by /browse, /random, and indirectly /search."""
    room = page.room
    if len(room) > 32:
        room_short = f"{room[:14]}…{room[-14:]}"
    else:
        room_short = room

    # Bookmark this room and the prev/next rooms so the UI can use compact
    # identifiers everywhere, including for pagination links. Three rooms is
    # the worst case (page-1 in different room, current room, page+1 in
    # different room); usually two of these are the same room as `page.room`.
    bookmark_hash = bookmarks.remember(room)
    compact_identifier = f"@{bookmark_hash}.{page.wall}.{page.shelf}.{page.book}.{page.page}"

    def _to_compact(full_id: str) -> str:
        room_part, _, tail = full_id.partition(".")
        h = bookmarks.remember(room_part)
        return f"@{h}.{tail}"

    return {
        "identifier":          page.identifier,
        "compact_identifier":  compact_identifier,
        "content":             page.content,
        "room":                room,
        "room_short":          room_short,
        "wall":                page.wall,
        "shelf":               page.shelf,
        "book":                page.book,
        "page":                page.page,
        "prev":                page.prev_identifier,
        "prev_compact":        _to_compact(page.prev_identifier),
        "next":                page.next_identifier,
        "next_compact":        _to_compact(page.next_identifier),
        "highlight":           highlight,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

VALID_MODES = {"empty", "chars", "words"}


@app.post("/search")
@limiter.limit(RATE_LIMIT)
async def search_endpoint(
    request: Request,
    content: str = Form(..., min_length=1, max_length=MAX_SEARCH_INPUT),
    mode: str = Form(default="empty"),
) -> dict:
    """Find an identifier whose page contains the given content."""
    if mode not in VALID_MODES:
        raise HTTPException(status_code=422, detail=f"mode must be one of {sorted(VALID_MODES)}")

    sanitized = B.sanitize_for_search(content)
    if not sanitized.strip():
        raise HTTPException(status_code=422, detail="Content was empty after sanitization")

    try:
        result = S.search(sanitized, mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    consts = request.app.state.babel
    bookmarks: BookmarkStore = request.app.state.bookmarks

    identifier = B.lookup_content(result.book, consts, page=result.page)
    room = identifier.split(".", 1)[0]
    bookmark_hash = bookmarks.remember(room)
    compact_identifier = (
        f"@{bookmark_hash}.{identifier.split('.', 1)[1]}"
    )

    return {
        "identifier":          identifier,
        "compact_identifier":  compact_identifier,
        "page":                result.page,
        "mode":                mode,
        "sanitized":           sanitized,
        "highlight":           list(result.highlight) if result.highlight else None,
    }


@app.post("/encode")
@limiter.limit(RATE_LIMIT)
async def encode_endpoint(
    request: Request,
    content: str = Form(..., min_length=1, max_length=MAX_SEARCH_INPUT),
    mode: str = Form(default="empty"),
) -> dict:
    """Like /search, but also bookmarks the room and produces a QR.

    Babel identifiers can stretch to over a million characters — too long for a
    QR code. We solve this the same way babel-master does: hash the room into a
    short identifier and persist the mapping locally. The QR encodes the
    compact `@HASH.W.S.B.P` form; the user (or anyone with this server) can
    later browse it by feeding the hashed form back through /browse.
    """
    if mode not in VALID_MODES:
        raise HTTPException(status_code=422, detail=f"mode must be one of {sorted(VALID_MODES)}")

    sanitized = B.sanitize_for_search(content)
    if not sanitized.strip():
        raise HTTPException(status_code=422, detail="Content was empty after sanitization")

    try:
        result = S.search(sanitized, mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    consts = request.app.state.babel
    bookmarks: BookmarkStore = request.app.state.bookmarks

    full_identifier = B.lookup_content(result.book, consts, page=result.page)
    room, _, tail = full_identifier.partition(".")  # split off "wall.shelf.book.page"

    bookmark_hash = bookmarks.remember(room)
    compact_identifier = f"@{bookmark_hash}.{tail}"

    qr_base64 = _qr_for_identifier(compact_identifier)

    return {
        "identifier":            full_identifier,
        "compact_identifier":    compact_identifier,
        "page":                  result.page,
        "mode":                  mode,
        "sanitized":             sanitized,
        "highlight":             list(result.highlight) if result.highlight else None,
        "qr_base64":             qr_base64,
        "qr_too_long":           not qr_base64,
    }


@app.post("/browse")
@limiter.limit(RATE_LIMIT)
async def browse_endpoint(
    request: Request,
    identifier: str = Form(..., min_length=9),  # at minimum "1.1.1.1.1"
) -> dict:
    """Return the page at `identifier`, plus prev/next links and metadata.

    Accepts both forms:
      * Full:    `<long_room>.WALL.SHELF.BOOK.PAGE`
      * Hashed:  `@HASH.WALL.SHELF.BOOK.PAGE` (resolved via bookmark store)

    Hashed identifiers come from QR scans or shared links; if the hash isn't
    known to this server's bookmark store, we return 404 so the caller can
    distinguish "never saved here" from "malformed".
    """
    consts = request.app.state.babel
    bookmarks: BookmarkStore = request.app.state.bookmarks

    raw = identifier.strip()

    # Resolve hashed form before validation — full path goes through parse_identifier.
    if is_hashed_identifier(raw):
        try:
            hash_part, suffix = split_hashed_identifier(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        room = bookmarks.lookup(hash_part)
        if room is None:
            raise HTTPException(
                status_code=404,
                detail="Bookmark hash unknown to this server. The full identifier "
                       "is required for first-time access.",
            )
        full_identifier = room + suffix
    else:
        full_identifier = raw

    try:
        page = B.generate_content(full_identifier, consts)
    except B.BabelIdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        log.exception("Browse failed")
        raise HTTPException(status_code=500, detail="Failed to render page")

    return _format_page_response(page, bookmarks=bookmarks)


@app.post("/random")
@limiter.limit(RATE_LIMIT)
async def random_endpoint(request: Request) -> dict:
    """Pick a uniformly random identifier and return its page."""
    consts = request.app.state.babel
    bookmarks: BookmarkStore = request.app.state.bookmarks

    identifier = B.random_identifier(consts)
    page = B.generate_content(identifier, consts)
    return _format_page_response(page, bookmarks=bookmarks)


@app.post("/qr")
@limiter.limit(RATE_LIMIT)
async def qr_endpoint(
    request: Request,
    identifier: str = Form(..., min_length=3, max_length=MAX_QR_IDENTIFIER_LEN),
) -> dict:
    """Render a QR for an arbitrary identifier string.

    The reader uses this to produce a QR for any page the user lands on, not
    just the freshly-encoded one. The identifier is treated as opaque text:
    we don't validate that it parses as a Babel identifier, since the QR
    payload could legitimately be a compact form (`@hash.W.S.B.P`) or a full
    one. Length is capped so we don't try to render an undecodeable QR.
    """
    qr_base64 = _qr_for_identifier(identifier.strip())
    if not qr_base64:
        raise HTTPException(status_code=422, detail="Identifier too long for a QR code.")
    return {"qr_base64": qr_base64, "identifier": identifier.strip()}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=RELOAD, log_level=LOG_LEVEL.lower())
