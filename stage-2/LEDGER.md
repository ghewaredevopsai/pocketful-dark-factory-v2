# Stage 1 requirements ledger

Status: `todo`, `done`, `n/a` (with reason). One line per normative statement.

## §1 Scope and invariants
- R1 done — Sum of all wallet balances always equals the total seeded by the last `POST /_test/reset` (all ops, concurrency, retries).
- R2 done — No wallet balance is ever negative, including transiently.
- R3 done — A payment request moves money at most once.
- R4 done — All amounts are exact integer minor units; money moves only between existing wallets (no deposit/withdraw endpoints).

## §2 Delivery and deployment
- R5 done — Deliver HTTP service, `Dockerfile`, `RUN.md` with a single build-and-start command, no manual setup.
- R6 done — Image runs alone with `-e PORT=<port>` and a port mapping; no outbound network at run time; seed/init inside the container.
- R7 done — Limits: 2 vCPU, 2 GiB memory, healthy within 60 s, 50 requests in flight, 5 s per request (10 s for test-control calls).
- R8 done — Runtime assets and dependencies are inside the image.
- R9 n/a — State need not survive a restart (ephemeral disk; in-memory store chosen).

## §3 Runtime contract
- R10 done — Listen on `0.0.0.0:$PORT`, default 8080.
- R11 done — `GET /health` → 200 `{"status":"ok"}` once ready, within 60 s.
- R12 done — `POST /_test/reset` with fixture → 204; replaces all state; later requests see only the fixture; repeatable; no auth.
- R13 done — Requests/responses `application/json; charset=utf-8`.
- R14 done — Response timestamps RFC 3339 with explicit offset.
- R15 done — Unknown body fields ignored; unknown query parameters ignored.
- R16 done — IDs opaque strings, ≤ 64 characters.

## §4 Model
- R17 done — One currency from the fixture; amounts are integer minor units; `minor_units` ∈ {0,2,3}.
- R18 done — Amounts: JSON `1000`, `1000.0`, `1e3` are the same valid integral value; booleans and strings are not numbers.
- R19 done — Handle unique, `^[a-z0-9_]{1,20}$`, never changes.
- R20 done — Signup handle derived from email local part: lowercase, chars outside `[a-z0-9_]` → `_`, truncate to 20.
- R21 done — New users start at balance 0 and can immediately receive and be asked for money.
- R22 done — Payment moves money immediately and atomically; sent directly or by paying a request.
- R23 done — Request status `pending` then exactly one of `paid`/`declined`/`cancelled`; only payer pays/declines; only requester cancels.
- R24 done — Request may exceed payer balance; paying while short is 409 `insufficient_funds`, changes nothing; later payable.
- R25 done — Visibility belongs to the payment, chosen by the payer; requests have no visibility and never appear in another's feed.
- R26 done — Feed: payment visible iff `public` or caller is sender/receiver; requests never in `/activity`; `/requests` only where caller is requester or payer.
- R27 done — A split is not a feed item; its requests are visible only to their two parties.
- R28 done — Visibility is one value seen identically by both parties and everyone else; `private` is not hidden from its receiver.
- R29 done — `amount` ≤ 1000000000 per request; no balance outside ±2^53; exact arithmetic (Python int, no floats in ledger).
- R30 done — Fixture format: currency, minor_units, users (id,email,password,display_name,handle,balance), payments, requests; `settlement_operator_ids` (default []).
- R31 done — Seeded users can log in immediately with the given password.
- R32 done — Fixture `balance` is post-payment; seeded payments are not replayed against balances.
- R33 done — Fixture balance < 0 → 422 `validation_failed` from reset, state unchanged.

## §5 Errors
- R34 done — Every 4xx/5xx body is `{"error":{"code":...,"message":...}}`.
- R35 done — 400 `malformed_request`: unparseable body or field of wrong JSON type.
- R36 done — 400 `missing_idempotency_key`: header absent or empty.
- R37 done — 401 `unauthenticated`: missing/malformed/unknown bearer token.
- R38 done — 403 `forbidden`: authenticated but not permitted.
- R39 done — 404 `not_found`: no such resource or not visible to caller.
- R40 done — 409 `idempotency_key_reuse`: same key, same caller, different body.
- R41 done — 422 `validation_failed`: missing required field/query param, or rule violated with no more specific code; invalid format/out-of-range values incl. invalid dates, negative counts, over max/length.
- R42 done — Invalid `amount` (incl. strings, booleans), non-string `note` (incl. null), visibility not public/private → 422 `validation_failed`; omission selects defaults.
- R43 done — Integer query params must be plain decimal digits; `1e9`, `4.0`, `+4` → 422.
- R44 done — `Idempotency-Key` length 1..255, else 422 (empty → 400 per R36).
- R45 done — `limit` integer 1..200, else 422.
- R46 done — `offset` integer ≥ 0, else 422.
- R47 done — No 5xx, including under concurrent load.

## §6 Authentication
- R48 done — `POST /auth/signup` {email,password,display_name} → 201 {user_id,display_name,token}.
- R49 done — `POST /auth/login` {email,password} → 200 {user_id,display_name,token}.
- R50 done — Email already registered → 409 `email_taken`.
- R51 done — Password < 8 chars → 422 `validation_failed`.
- R52 done — Email not `local@domain` → 422 `validation_failed`.
- R53 done — Wrong password or unknown email on login → 401 `unauthenticated`.
- R54 done — Derived handle already taken → 409 `handle_taken`, no account created.
- R55 done — All other endpoints except /health, /_test/*, signup, login require `Authorization: Bearer <token>`.
- R56 done — Tokens do not expire; multiple valid tokens per account.
- R57 done — Passwords stored with a password-hashing function (scrypt); never plaintext.

## §7 Idempotency (POST /payments, /requests, /requests/{id}/pay, /splits, /settlements)
- R58 done — Key scoped per authenticated user; same string across users does not interact.
- R59 done — Replay = same user + method + path + body; same key/body on a different path is a new request.
- R60 done — First use → normal 201.
- R61 done — Replay → 200, body identical (as JSON value) to original.
- R62 done — Same key, different body → 409 `idempotency_key_reuse`.
- R63 done — Key whose original failed with 4xx is treated as first use.
- R64 done — Same body = same JSON value after parsing (key order/whitespace irrelevant).
- R65 done — Concurrent identical requests on an unused key: exactly one 201, others 200 same body, effect once.
- R66 done — Replay returns original response even after resource changed/cancelled; no further state change.
- R67 done — After body parses as object and caller is authenticated, a claimed key is resolved before field validation and resource checks.

## §8 API
- R68 done — `GET /me` → {user_id,display_name,handle,balance,currency,minor_units}.
- R69 done — `POST /payments` {to_handle,amount,note?="",visibility?="public"} → 201 payment body (payment_id, from/to ids+handles, amount, currency, note, visibility, request_id:null, created_at; settlement_id:null per §11).
- R70 done — Payments: balance < amount → 409 `insufficient_funds`.
- R71 done — Payments: amount < 1, > 1000000000 or non-integer → 422.
- R72 done — Payments: to own handle → 422 `self_payment`.
- R73 done — Payments: note > 200 chars → 422.
- R74 done — Payments: bad visibility → 422.
- R75 done — Payments: unknown handle → 404 `not_found`.
- R76 done — Debit and credit one atomic step; failed payment leaves no trace.
- R77 done — `note` stored/returned verbatim; Unicode/emoji round-trip byte for byte.
- R78 done — `POST /requests` {payer_handle,amount,note?} → 201 request body (request_id, requester/payer ids+handles, amount, currency, note, status pending, payment_id null, created_at).
- R79 done — Requests: amount out of range/non-integer → 422.
- R80 done — Requests: own handle → 422 `self_request`.
- R81 done — Requests: note > 200 → 422.
- R82 done — Requests: unknown handle → 404.
- R83 done — Requests: payer balance not checked at creation.
- R84 done — `POST /requests/{id}/pay` {visibility?="public"} → 201 payment with request_id; request becomes paid with payment_id.
- R85 done — Pay: `{}` vs `{"visibility":"public"}` are different bodies for idempotency.
- R86 done — Pay: not pending → 409 `request_not_pending`.
- R87 done — Pay: payer balance < amount → 409 `insufficient_funds`.
- R88 done — Pay: caller not payer → 403 `forbidden`.
- R89 done — Pay: unknown request → 404.
- R90 done — Pay replay → 200 original payment body even when request already paid; no money moved; never 409 request_not_pending.
- R91 done — `POST /requests/{id}/decline`: payer only, no key; 200 request with declined; repeat decline 200; paid/cancelled → 409 `request_not_pending`; non-payer 403.
- R92 done — `POST /requests/{id}/cancel`: requester only, no key; 200 cancelled; repeat 200; paid/declined → 409; non-requester 403.
- R93 done — `GET /requests`: only caller's requests, newest first by created_at.
- R94 done — `direction` incoming/outgoing/absent; `status` one of four or absent; unknown values → 422.
- R95 done — `limit` default 50 (1..200), `offset` default 0 (≥0); `has_more` true iff items beyond the last returned; body `{"requests":[...],"has_more":bool}`.
- R96 done — `POST /splits` {amount,participant_handles,note?} → 201 {split_id,amount,currency,note,shares,requests,created_at}.
- R97 done — Splits: caller may or may not be in participants; one pending request per non-caller participant, caller as requester.
- R98 done — Splits: `shares` covers every participant incl. caller in given order, sums to amount; `requests` same order, excluding caller.
- R99 done — Splits: amount out of range/non-integer → 422.
- R100 done — Splits: participant_handles empty or duplicate → 422.
- R101 done — Splits: note > 200 → 422.
- R102 done — Splits: any unknown handle → 404.
- R103 done — Splits: caller-only split valid, one share, `requests: []`; no balance checks.
- R104 done — `GET /activity?limit&offset` → `{"payments":[...],"has_more":bool}`, feed-contract visible payments, newest first; same limit/offset rules.

## §9 Money and rounding
- R105 done — Shares whole minor units, sum exactly to amount, differ by ≤ 1; larger shares to first participants in given order.
- R106 done — Table rows: 1000/3 → 334,333,333; 1/3 → 1,0,0; 10/3 → 4,3,3; 999/3 → 333,333,333; 5/5 → 1×5.
- R107 done — Zero share is legal and still creates a request (amount 0).
- R108 done — Each split independent of previous ones; balances still sum to seeded total after paying splits.

## §10 Export and import
- R109 done — `GET /_test/export` (no auth) → 200 {track:"pocketful",format_version:1,state:{...}}.
- R110 done — `POST /_test/import` (no auth) takes the whole export object, atomically replaces state, 204; accepts an unchanged export.
- R111 done — Import is replacement, not merge; repeat import restores without duplication; no dependency on source process/files/address.
- R112 done — Import: invalid JSON → §5 (400 malformed_request); missing fields, wrong track/version, invalid state → 422 validation_failed, destination unchanged.
- R113 done — Export is an atomic read-only snapshot; later writes don't change it.
- R114 done — Import preserves accounts, password hashes, tokens, currency, balances, payments, requests, permissions, idempotency records with original responses; nothing regenerated/replayed; failed keys stay reusable.
- R115 done — Import removes all previous destination data and credentials; reset clears imported state.

## §11 Atomic net settlements
- R116 done — Fixture `settlement_operator_ids` (default []); operators may settle across any wallets but gain no access to others' requests or private activity.
- R117 done — `POST /settlements` needs idempotency key; no token → 401; non-operator → 403 `forbidden`.
- R118 done — Body `{"transfers":[{from_handle,to_handle,amount,note?,visibility?}]}`, 1..32 entries; malformed batch shape → 422 `validation_failed`; unknown fields ignored.
- R119 done — Each entry: payment amount/note/visibility rules; unknown handle → 404; self transfer → 422 `self_payment`; entry errors in input order, before insufficient funds.
- R120 done — Affordable iff every wallet's net post-settlement balance ≥ 0; else 409 `insufficient_funds`.
- R121 done — All-or-nothing commit; failed validation claims no key and creates no payment.
- R122 done — 201 {settlement_id, committed_at, payments (input order)}; members are ordinary payments with settlement_id, request_id null, created_at = committed_at; non-members have settlement_id null.
- R123 done — Members follow normal feed visibility; replay → 200 with original complete response.
- R124 done — Reset/import preserve operator permissions, payments, requests, settlement membership, retry responses.

## Added on coordinator review (msg ae47dd3f)
- R125 done — Seeded payments/requests (ids, notes, visibility, statuses incl. paid/declined/cancelled with payment_id) readable via /activity and /requests under normal rules; not replayed against balances. Test: `Reset.test_seeded_state_readable`.
- R126 done — Malformed fixture (wrong types, bad minor_units, duplicate handle/email/id, bad handle, unknown user ids in payments/requests/operators) → 4xx (400 wrong type, 422 otherwise), state unchanged. Test: `Reset.test_malformed_fixtures`.
- R127 done — Seeded users get working tokens via login; handles immutable (no endpoint changes them).
- R128 done — Failed (4xx) idempotent request does not claim the key, including under concurrency. Tests: `Idempotency.test_failed_key_reusable`, `test_concurrent_failures_do_not_claim`, `Settlements.test_errors`.
- R129 done — Idempotency-Key > 255 → 422 only after auth (no token → 401); absent/empty → 400. Test: `Idempotency.test_header_rules`.
- R130 done — Same-second payments ordered by (timestamp, sequence), strictly increasing clock, so pages have no duplicates/gaps without concurrent writes. Test: `Feed.test_paging_same_second_consistent`.
- R131 done — Critical sections are short in-memory work; scrypt (N=2^14, r=8) runs outside the lock, at most 4 at a time; 50 concurrent logins < 5 s. Test: `Auth.test_concurrent_logins_fast`.

## Interpretation decisions
- Non-payer third party calling pay/decline gets 403 `forbidden` (table: "caller is not the request's payer"); same for non-requester cancel.
- In `/settlements`, a non-string handle inside a transfer is 422 `validation_failed` (malformed batch shape), and the operator check (403) precedes the Idempotency-Key check.
- Fixture records without `created_at` take increasing instants in fixture order (last listed = newest); a fixture `created_at`, if given, is used.
- Email uniqueness and login are case-insensitive; the derived handle lowercases ASCII letters and maps every other non-`[a-z0-9_]` character to `_`.
- Empty request body (ruling fd77ec53): unparseable → 400 `malformed_request` on every JSON endpoint, as is a body
  that parses to a non-object. Exception: `POST /requests/{id}/pay`, whose body is optional, treats an empty body as
  `{}`; for idempotency it is the same value as `{}` (not distinct). decline/cancel ignore their body.
- Idempotency scope is (user, path, key); "same body" compares parsed JSON values exactly (`1000` = `1000.0` = `1e3`; `true` ≠ `1`).

## Serialization point
One process, one in-memory store, one `threading.Lock` held for the full duration of every state read-modify-write
(and every snapshot read). That lock is the single serialization point; see `RUN.md`.
