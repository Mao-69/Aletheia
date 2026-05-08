# Library of Aletheia

A faithful, complete implementation of Borges's Library of Babel as a
deterministic bijection: every page in the library exists at exactly one
address, and every address yields exactly one page. Content is never stored
— it is *computed* from the address.

This is a Python port of [Tom Snelling's babel](https://libraryofbabel.app),
extended with QR codes, an in-process bookmark store, and a 42-character
alphabet (a–z, six punctuation marks, space, and digits 0–9).

> _Aletheia_ (ἀλήθεια) — Greek for "disclosure", "unconcealment". The page
> is already there. The library only reveals its address.

---

## How it works

Every book in the library is 1,312,000 characters long (40 lines × 80 chars
× 410 pages). Each character is one of 42 symbols. So the total number of
distinct books is

    N = 42^1,312,000

— a 7-million-bit integer.

Each book is given a unique sequential index `s` in `[1, N]`. The address —
shown to the user as `room.wall.shelf.book.page` — is just `s` rewritten in
mixed bases (room is base-32, wall ∈ 1–4, shelf ∈ 1–5, book ∈ 1–32, page ∈
1–410).

### The bijection

We pick two integers once at startup:

* **C** — random in `[1, N)` with `gcd(C, N) = 1` (i.e. coprime to N).
* **I** — the modular inverse of C mod N: `C × I ≡ 1 (mod N)`.

Given the sequential index of a book, the contents are:

    book_value = (s × C) mod N

interpreted as a base-42 number whose digits are characters in our alphabet.

Going backwards — finding which address contains some text `t` — is
symmetric:

    s = (t_as_base42 × I) mod N

Because `C` is invertible mod `N`, this map is a *bijection*: distinct
indices yield distinct contents and distinct contents come from distinct
indices. No two books are the same; nothing is missing.

The proof that `gcd(C, N) = 1` is sufficient is on
[Math StackExchange](https://math.stackexchange.com/questions/3022985);
the original framing is from Eric Lippert's
[A practical use of multiplicative inverses](https://ericlippert.com/2013/11/14/a-practical-use-of-multiplicative-inverses).

### Why GMP

A single big-integer modular multiplication at this size takes ~80s with
Python's built-in `int`. With [GMP](https://gmplib.org/) (via the
[`gmpy2`](https://gmpy2.readthedocs.io) Python wrapper), it takes ~100ms.
GMP is therefore a hard dependency.

### Why a bookmark store

For very short input phrases, the resulting address has a *very long* room
component — up to 1.4 million characters. That's because most of the book
(everywhere outside your phrase) is whitespace, and a near-zero base-42
number has a near-zero magnitude, but a near-zero whose first few "digits"
happen to be `space space space …` is *not* a small integer.

A QR code can hold about 3,000 characters. So when the user creates a book
with content that produces a long address, we hash the room into a 16-hex-
character SHA-256 prefix, persist `hash → room` in a SQLite file
(`bookmarks.sqlite3`), and use the compact form `@HASH.W.S.B.P` for the QR
and for sharing. When someone hands us a compact identifier, we resolve the
hash back to the full room. (This is exactly how
[babel](https://libraryofbabel.app) handles it.)

If you load a compact identifier on a server that's never seen the
underlying room, you'll get a 404 — bookmark databases are local. Pass the
full identifier and the server will record the room itself.

---

## Architecture

```
aletheia_v2/
├── app.py               FastAPI server (endpoints, middleware, lifespan)
├── babel_lib.py         The math: bijection, content<->int, identifier parsing
├── search_modes.py      Three filler styles for search input (empty/chars/words)
├── bookmark_store.py    SHA-256 hash <-> long-room mapping (SQLite, WAL)
├── qr_render.py         Vectorised QR rendering with the cyberpunk-bunny overlay
├── numbers.bin          Cached C and I (regenerated on first run if missing)
├── bookmarks.sqlite3    Local bookmark store (created on first run)
├── static/
│   ├── index.html       Single-page UI (no inline scripts; CSP-strict)
│   ├── styles.css       Cyberpunk-terminal aesthetic
│   ├── app.js           View switching, fetch wrappers, reader, QR scanner
│   ├── wabbit.gif       Animated pixel-art bunny background (desktop)
│   ├── bunny.png        Static fallback (mobile)
│   └── cyberpunk_bunny.png  Overlaid on QR images
├── test_aletheia.py     Pytest suite (47 tests covering math + HTTP)
└── requirements.txt
```

---

## Endpoints

All accept `multipart/form-data` POSTs (or `application/x-www-form-urlencoded`).

| Method | Path      | Body                              | Returns |
| ------ | --------- | --------------------------------- | ------- |
| GET    | `/`       | —                                 | The SPA |
| GET    | `/health` | —                                 | `{"status": "ok"}` |
| POST   | `/search` | `content`, `mode`                 | `{identifier, compact_identifier, page, mode, sanitized, highlight}` |
| POST   | `/encode` | `content`, `mode`                 | `…` + `qr_base64` (PNG bytes, base64) |
| POST   | `/browse` | `identifier` (full **or** `@hash`)| `{content, room, room_short, wall, shelf, book, page, prev, next, prev_compact, next_compact, identifier, compact_identifier, highlight}` |
| POST   | `/random` | —                                 | same shape as `/browse` |
| POST   | `/qr`     | `identifier` (any string ≤ 1500c) | `{qr_base64, identifier}` |

`mode` is one of `empty` (default), `chars`, `words`. See
`search_modes.py` for what each does.

`highlight`, when present, is `[start_line, start_col, end_line, end_col]`
in **page-local coordinates** (0-indexed). The frontend draws a gold box
over the matched span on the rendered page.

---

## Running it

### Local

```bash
pip install -r requirements.txt
python app.py
# → http://127.0.0.1:8000
```

First boot generates the `C` and `I` constants (~1.5 seconds, one-time).

### Configuration

Environment variables:

| Variable          | Default       | Effect |
| ----------------- | ------------- | ------ |
| `HOST`            | `127.0.0.1`   | Bind address (use `0.0.0.0` to expose) |
| `PORT`            | `8000`        | Bind port |
| `RELOAD`          | `0`           | Set to `1` for uvicorn auto-reload |
| `LOG_LEVEL`       | `INFO`        | uvicorn / app log level |
| `RATE_LIMIT`      | `30/minute`   | Per-IP rate cap on every endpoint |
| `ALLOWED_ORIGINS` | (off)         | Comma-separated CORS allowlist; off by default |

### Tests

```bash
pip install pytest httpx
python -m pytest test_aletheia.py -v
```

The suite runs in ~45s — the bulk is the bijection round-trips doing real
~7M-bit modular arithmetic. There's no mocking; if the math breaks, the
tests notice.

---

## Security posture

* No `innerHTML` anywhere — all server-derived strings hit the DOM via
  `textContent`. Reader pages are 3,200 characters of arbitrary text from
  the library; they are rendered as text, not HTML.
* CSP forbids inline scripts and forbids any frame ancestor. `script-src`
  permits `self` plus `unpkg.com` (where `html5-qrcode` is loaded).
* `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Permissions-Policy: camera=(self), microphone=(), geolocation=()`.
* CORS is **off by default**; set `ALLOWED_ORIGINS` to enable.
* Per-IP rate limiting on every endpoint via `slowapi`.
* Identifier validation: room must be base-32 only; wall/shelf/book/page
  must be in their nominal ranges. Invalid inputs return 422.
* Bookmark hashes are 16 hex characters (64 bits). Collisions are
  *theoretically* possible at scale; in practice the working set will never
  approach `2^32` rooms.

---

## Lineage

* Borges, *La biblioteca de Babel* (1941) — the original.
* [libraryofbabel.info](https://libraryofbabel.info) — Jonathan Basile's
  every-unique-page version. Beautiful, but pages don't compose into books.
* [babel](https://github.com/tdjsnelling/babel) — Tom Snelling's
  every-unique-book version, written in TypeScript with
  [`gmp-wasm`](https://github.com/Daninet/gmp-wasm). The mathematical
  approach here is a direct port.
* This: same bijection, in Python with `gmpy2`, plus a 42-character
  alphabet (digits added), a QR layer with bookmark hashing, and a
  cyberpunk-terminal UI.
