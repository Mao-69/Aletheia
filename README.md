# Library of Aletheia

> _Aletheia_ (ἀλήθεια) — Greek for "disclosure", "unconcealment". The page
> is already there. The library only reveals its address.

<img width="1383" height="697" alt="Screenshot From 2026-05-07 21-49-20" src="https://github.com/user-attachments/assets/92ce9d02-34fe-4ecd-9229-951b39d80ed8" />

This is a Python port of Babel,
extended with QR codes, an in-process bookmark store, and a 42-character
alphabet (a–z, six punctuation marks, space, and digits 0–9).

---


<img width="1352" height="614" alt="Screenshot From 2026-05-07 23-17-18" src="https://github.com/user-attachments/assets/7553b81b-2afd-42c5-aa83-a5e619771f44" />

<img width="1352" height="614" alt="Screenshot From 2026-05-07 23-18-21" src="https://github.com/user-attachments/assets/36a2d7fe-224e-4960-89b7-fb41ccbfe988" />


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

## Running it

### Local

```bash
pip install -r requirements.txt
python app.py
# → http://127.0.0.1:8000
```

First boot generates the `C` and `I` constants (~1.5 seconds, one-time).

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
