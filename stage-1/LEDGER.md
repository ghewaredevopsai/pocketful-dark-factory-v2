# Stage 1 requirements ledger

Status: `todo`, `done`, `n/a` (with reason). One line per normative statement.

## §1 Scope and invariants
- R1 todo — Sum of all wallet balances always equals the total seeded by the last `POST /_test/reset` (all ops, concurrency, retries).
- R2 todo — No wallet balance is ever negative, including transiently.
- R3 todo — A payment request moves money at most once.
- R4 todo — All amounts are exact integer minor units; money moves only between existing wallets (no deposit/withdraw endpoints).

## §2 Delivery and deployment
- R5 todo — Deliver HTTP service, `Dockerfile`, `RUN.md` with a single build-and-start command, no manual setup.
- R6 todo — Image runs alone with `-e PORT=<port>` and a port mapping; no outbound network at run time; seed/init inside the container.
- R7 todo — Limits: 2 vCPU, 2 GiB memory, healthy within 60 s, 50 requests in flight, 5 s per request (10 s for test-control calls).
- R8 todo — Runtime assets and dependencies are inside the image.
- R9 n/a — State need not survive a restart (ephemeral disk; in-memory store chosen).

## §3 Runtime contract
- R10 todo — Listen on `0.0.0.0:$PORT`, default 8080.
- R11 todo — `GET /health` → 200 `{"status":"ok"}` once ready, within 60 s.
- R12 todo — `POST /_test/reset` with fixture → 204; replaces all state; later requests see only the fixture; repeatable; no auth.
- R13 todo — Requests/responses `application/json; charset=utf-8`.
- R14 todo — Response timestamps RFC 3339 with explicit offset.
- R15 todo — Unknown body fields ignored; unknown query parameters ignored.
- R16 todo — IDs opaque strings, ≤ 64 characters.

## §4 Model
- R17 todo — One currency from the fixture; amounts are integer minor units; `minor_units` ∈ {0,2,3}.
- R18 todo — Amounts: JSON `1000`, `1000.0`, `1e3` are the same valid integral value; booleans and strings are not numbers.
- R19 todo — Handle unique, `^[a-z0-9_]{1,20}$`, never changes.
- R20 todo — Signup handle derived from email local part: lowercase, chars outside `[a-z0-9_]` → `_`, truncate to 20.
- R21 todo — New users start at balance 0 and can immediately receive and be asked for money.
- R22 todo — Payment moves money immediately and atomically; sent directly or by paying a request.
- R23 todo — Request status `pending` then exactly one of `paid`/`declined`/`cancelled`; only payer pays/declines; only requester cancels.
- R24 todo — Request may exceed payer balance; paying while short is 409 `insufficient_funds`, changes nothing; later payable.
- R25 todo — Visibility belongs to the payment, chosen by the payer; requests have no visibility and never appear in another's feed.
- R26 todo — Feed: payment visible iff `public` or caller is sender/receiver; requests never in `/activity`; `/requests` only where caller is requester or payer.
- R27 todo — A split is not a feed item; its requests are visible only to their two parties.
- R28 todo — Visibility is one value seen identically by both parties and everyone else; `private` is not hidden from its receiver.
- R29 todo — `amount` ≤ 1000000000 per request; no balance outside ±2^53; exact arithmetic (Python int, no floats in ledger).
- R30 todo — Fixture format: currency, minor_units, users (id,email,password,display_name,handle,balance), payments, requests; `settlement_operator_ids` (default []).
- R31 todo — Seeded users can log in immediately with the given password.
- R32 todo — Fixture `balance` is post-payment; seeded payments are not replayed against balances.
- R33 todo — Fixture balance < 0 → 422 `validation_failed` from reset, state unchanged.

## §5 Errors
- R34 todo — Every 4xx/5xx body is `{"error":{"code":...,"message":...}}`.
- R35 todo — 400 `malformed_request`: unparseable body or field of wrong JSON type.
- R36 todo — 400 `missing_idempotency_key`: header absent or empty.
- R37 todo — 401 `unauthenticated`: missing/malformed/unknown bearer token.
- R38 todo — 403 `forbidden`: authenticated but not permitted.
- R39 todo — 404 `not_found`: no such resource or not visible to caller.
- R40 todo — 409 `idempotency_key_reuse`: same key, same caller, different body.
- R41 todo — 422 `validation_failed`: missing required field/query param, or rule violated with no more specific code; invalid format/out-of-range values incl. invalid dates, negative counts, over max/length.
- R42 todo — Invalid `amount` (incl. strings, booleans), non-string `note` (incl. null), visibility not public/private → 422 `validation_failed`; omission selects defaults.
- R43 todo — Integer query params must be plain decimal digits; `1e9`, `4.0`, `+4` → 422.
- R44 todo — `Idempotency-Key` length 1..255, else 422 (empty → 400 per R36).
- R45 todo — `limit` integer 1..200, else 422.
- R46 todo — `offset` integer ≥ 0, else 422.
- R47 todo — No 5xx, including under concurrent load.

## §6 Authentication
- R48 todo — `POST /auth/signup` {email,password,display_name} → 201 {user_id,display_name,token}.
- R49 todo — `POST /auth/login` {email,password} → 200 {user_id,display_name,token}.
- R50 todo — Email already registered → 409 `email_taken`.
- R51 todo — Password < 8 chars → 422 `validation_failed`.
- R52 todo — Email not `local@domain` → 422 `validation_failed`.
- R53 todo — Wrong password or unknown email on login → 401 `unauthenticated`.
- R54 todo — Derived handle already taken → 409 `handle_taken`, no account created.
- R55 todo — All other endpoints except /health, /_test/*, signup, login require `Authorization: Bearer <token>`.
- R56 todo — Tokens do not expire; multiple valid tokens per account.
- R57 todo — Passwords stored with a password-hashing function (scrypt); never plaintext.

## §7 Idempotency (POST /payments, /requests, /requests/{id}/pay, /splits, /settlements)
- R58 todo — Key scoped per authenticated user; same string across users does not interact.
- R59 todo — Replay = same user + method + path + body; same key/body on a different path is a new request.
- R60 todo — First use → normal 201.
- R61 todo — Replay → 200, body identical (as JSON value) to original.
- R62 todo — Same key, different body → 409 `idempotency_key_reuse`.
- R63 todo — Key whose original failed with 4xx is treated as first use.
- R64 todo — Same body = same JSON value after parsing (key order/whitespace irrelevant).
- R65 todo — Concurrent identical requests on an unused key: exactly one 201, others 200 same body, effect once.
- R66 todo — Replay returns original response even after resource changed/cancelled; no further state change.
- R67 todo — After body parses as object and caller is authenticated, a claimed key is resolved before field validation and resource checks.

## §8 API
- R68 todo — `GET /me` → {user_id,display_name,handle,balance,currency,minor_units}.
- R69 todo — `POST /payments` {to_handle,amount,note?="",visibility?="public"} → 201 payment body (payment_id, from/to ids+handles, amount, currency, note, visibility, request_id:null, created_at; settlement_id:null per §11).
- R70 todo — Payments: balance < amount → 409 `insufficient_funds`.
- R71 todo — Payments: amount < 1, > 1000000000 or non-integer → 422.
- R72 todo — Payments: to own handle → 422 `self_payment`.
- R73 todo — Payments: note > 200 chars → 422.
- R74 todo — Payments: bad visibility → 422.
- R75 todo — Payments: unknown handle → 404 `not_found`.
- R76 todo — Debit and credit one atomic step; failed payment leaves no trace.
- R77 todo — `note` stored/returned verbatim; Unicode/emoji round-trip byte for byte.
- R78 todo — `POST /requests` {payer_handle,amount,note?} → 201 request body (request_id, requester/payer ids+handles, amount, currency, note, status pending, payment_id null, created_at).
- R79 todo — Requests: amount out of range/non-integer → 422.
- R80 todo — Requests: own handle → 422 `self_request`.
- R81 todo — Requests: note > 200 → 422.
- R82 todo — Requests: unknown handle → 404.
- R83 todo — Requests: payer balance not checked at creation.
- R84 todo — `POST /requests/{id}/pay` {visibility?="public"} → 201 payment with request_id; request becomes paid with payment_id.
- R85 todo — Pay: `{}` vs `{"visibility":"public"}` are different bodies for idempotency.
- R86 todo — Pay: not pending → 409 `request_not_pending`.
- R87 todo — Pay: payer balance < amount → 409 `insufficient_funds`.
- R88 todo — Pay: caller not payer → 403 `forbidden`.
- R89 todo — Pay: unknown request → 404.
- R90 todo — Pay replay → 200 original payment body even when request already paid; no money moved; never 409 request_not_pending.
- R91 todo — `POST /requests/{id}/decline`: payer only, no key; 200 request with declined; repeat decline 200; paid/cancelled → 409 `request_not_pending`; non-payer 403.
- R92 todo — `POST /requests/{id}/cancel`: requester only, no key; 200 cancelled; repeat 200; paid/declined → 409; non-requester 403.
- R93 todo — `GET /requests`: only caller's requests, newest first by created_at.
- R94 todo — `direction` incoming/outgoing/absent; `status` one of four or absent; unknown values → 422.
- R95 todo — `limit` default 50 (1..200), `offset` default 0 (≥0); `has_more` true iff items beyond the last returned; body `{"requests":[...],"has_more":bool}`.
- R96 todo — `POST /splits` {amount,participant_handles,note?} → 201 {split_id,amount,currency,note,shares,requests,created_at}.
- R97 todo — Splits: caller may or may not be in participants; one pending request per non-caller participant, caller as requester.
- R98 todo — Splits: `shares` covers every participant incl. caller in given order, sums to amount; `requests` same order, excluding caller.
- R99 todo — Splits: amount out of range/non-integer → 422.
- R100 todo — Splits: participant_handles empty or duplicate → 422.
- R101 todo — Splits: note > 200 → 422.
- R102 todo — Splits: any unknown handle → 404.
- R103 todo — Splits: caller-only split valid, one share, `requests: []`; no balance checks.
- R104 todo — `GET /activity?limit&offset` → `{"payments":[...],"has_more":bool}`, feed-contract visible payments, newest first; same limit/offset rules.

## §9 Money and rounding
- R105 todo — Shares whole minor units, sum exactly to amount, differ by ≤ 1; larger shares to first participants in given order.
- R106 todo — Table rows: 1000/3 → 334,333,333; 1/3 → 1,0,0; 10/3 → 4,3,3; 999/3 → 333,333,333; 5/5 → 1×5.
- R107 todo — Zero share is legal and still creates a request (amount 0).
- R108 todo — Each split independent of previous ones; balances still sum to seeded total after paying splits.

## §10 Export and import
- R109 todo — `GET /_test/export` (no auth) → 200 {track:"pocketful",format_version:1,state:{...}}.
- R110 todo — `POST /_test/import` (no auth) takes the whole export object, atomically replaces state, 204; accepts an unchanged export.
- R111 todo — Import is replacement, not merge; repeat import restores without duplication; no dependency on source process/files/address.
- R112 todo — Import: invalid JSON → §5 (400 malformed_request); missing fields, wrong track/version, invalid state → 422 validation_failed, destination unchanged.
- R113 todo — Export is an atomic read-only snapshot; later writes don't change it.
- R114 todo — Import preserves accounts, password hashes, tokens, currency, balances, payments, requests, permissions, idempotency records with original responses; nothing regenerated/replayed; failed keys stay reusable.
- R115 todo — Import removes all previous destination data and credentials; reset clears imported state.

## §11 Atomic net settlements
- R116 todo — Fixture `settlement_operator_ids` (default []); operators may settle across any wallets but gain no access to others' requests or private activity.
- R117 todo — `POST /settlements` needs idempotency key; no token → 401; non-operator → 403 `forbidden`.
- R118 todo — Body `{"transfers":[{from_handle,to_handle,amount,note?,visibility?}]}`, 1..32 entries; malformed batch shape → 422 `validation_failed`; unknown fields ignored.
- R119 todo — Each entry: payment amount/note/visibility rules; unknown handle → 404; self transfer → 422 `self_payment`; entry errors in input order, before insufficient funds.
- R120 todo — Affordable iff every wallet's net post-settlement balance ≥ 0; else 409 `insufficient_funds`.
- R121 todo — All-or-nothing commit; failed validation claims no key and creates no payment.
- R122 todo — 201 {settlement_id, committed_at, payments (input order)}; members are ordinary payments with settlement_id, request_id null, created_at = committed_at; non-members have settlement_id null.
- R123 todo — Members follow normal feed visibility; replay → 200 with original complete response.
- R124 todo — Reset/import preserve operator permissions, payments, requests, settlement membership, retry responses.

## Serialization point
One process, one in-memory store, one `threading.Lock` held for the full duration of every state read-modify-write
(and every snapshot read). That lock is the single serialization point; see `RUN.md`.
