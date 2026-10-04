# Pocketful stage 1 — run

Build and start (from this folder):

```sh
docker build -t pocketful-s1 . && docker run --rm -e PORT=8080 -p 8080:8080 pocketful-s1
```

Health: `curl http://localhost:8080/health` → `{"status": "ok"}`.

Tests (start no container; the test file starts the server in-process):

```sh
python3 test_service.py
```

## Design in one paragraph

A single Python process (standard library only, no runtime dependencies) serves HTTP with one thread per
connection. All state lives in one in-memory `State` object. One `threading.Lock` (`LOCK` in `server.py`) is the
single serialization point: every read and every read-modify-write of balances, payments, requests, tokens and
idempotency records happens while it is held, so the idempotency claim, the balance check and the money movement
of a write are one atomic step and concurrent retries resolve to exactly one effect. Password hashing (scrypt)
runs outside the lock. Amounts are parsed from JSON as `Decimal` and stored as Python `int`; no floating point
touches money. All instants come from one UTC clock with microsecond precision, forced strictly increasing.
