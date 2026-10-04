# Pocketful stage 3 — run

Build and start (from this folder):

```sh
docker build -t pocketful-s3 . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful-s3
```

Health: `curl http://localhost:8080/health` → `{"status": "ok"}`.

Open http://localhost:8080/ in a browser (UI routes: `/`, `/requests`, `/split`, `/authorizations`, `/login`,
`/signup`). All assets (`static/`) are inside the image; fonts are the system font stack.

Tests (each starts the server in-process when no base URL is given; pass `http://host:port` to test a container):

```sh
python3 test_service.py            # API, concurrency, holds, export/import (incl. a real stage-1 export)
python test_ui.py                  # browser behaviours; needs Python Playwright + Chromium
```

## Design in one paragraph

A single Python process (standard library only, no runtime dependencies) serves HTTP with one thread per
connection. All state lives in one in-memory `State` object. One `threading.Lock` (`LOCK` in `server.py`) is the
single serialization point: every read and every read-modify-write of balances, payments, requests, tokens and
idempotency records happens while it is held, so the idempotency claim, the balance check and the money movement
of a write are one atomic step and concurrent retries resolve to exactly one effect. Password hashing (scrypt)
runs outside the lock. Amounts are parsed from JSON as `Decimal` and stored as Python `int`; no floating point
touches money. All instants come from one UTC clock with microsecond precision, forced strictly increasing. Each write
takes exactly one instant inside the lock; hold expiry, clock checks and the recorded time all use that instant.

Holds: every user carries `held`, the sum of the uncaptured remainders of their open authorizations, so
`available = balance - held`. Every funds check (payments, request pay, authorizations, settlement net debits)
uses `available`. Expiry is lazy: each locked operation first closes holds whose `expires_at <= now`, releasing
the remainder exactly once. The browser client keeps one Idempotency-Key per unchanged form body, so a double
submit or a retry after a lost response is a replay; refreshes carry a generation number and an older response
never overwrites a newer one.

History (stage 3): each payment keeps immutable revisions (amount, effective_at, recorded_at, reason); each user keeps
an opening balance; each hold keeps its lifecycle (creation, captures, close). `as_of`, `known_at` and statements are
computed from those records under the same lock: pick each payment's latest revision recorded at or before
`known_at`, apply it at its effective time, and add hold events known by then. Statement snapshots store the
computed result and are paged from it.
