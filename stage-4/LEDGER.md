# Stage 4 requirements ledger

R1–R131 come from stage 1, R132–R226 and M1/M2 from stage 2; all carry over and are re-verified by the carried tests.
R227–R274 come from stage 3 (with the stage-3 review fixes B1/B2); all carry over and are re-verified by the carried
tests. R275+ are new in stage 4.

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


## Stage 2 — UI routes and auth screens
- R132 done — GET `/`, `/requests`, `/split`, `/signup`, `/login`, `/authorizations` reachable by URL (HTML).
- R133 done — `/requests` and `/authorizations`: HTML for `Accept: text/html`, JSON otherwise.
- R134 done — Other screens reachable through the UI; consistent navigation across routes.
- R135 done — Signup testids: `signup-email`, `signup-password`, `signup-display-name`, `signup-submit`.
- R136 done — Login testids: `login-email`, `login-password`, `login-submit`.
- R137 done — `auth-error` present only when there is an error.
- R138 done — `current-user` visible on every screen when signed in, text contains display name.
- R139 done — `current-handle` text exactly the handle (no `@`, no words).
- R140 done — `logout-button` signs out (current-user disappears).

## Stage 2 — Balance, pay and request forms (`/`)
- R141 done — `wallet-balance` text exactly formatted total, `data-amount` = minor units.
- R142 done — Formatted amount: exactly `minor_units` decimals, one space, currency code; `minor_units: 0` → no decimal point; no sign.
- R143 done — `pay-handle`, `pay-amount` (decimal string), `pay-note`, `pay-visibility` (option values `public`/`private`), `pay-submit`.
- R144 done — `pay-error` shown when the payment is refused, incl. insufficient funds.
- R145 done — Request form: `request-handle`, `request-amount`, `request-note`, `request-submit`; `request-error` when refused.
- R146 done — Decimal parsing: `15.00`/`15` → 1500, `15.5` → 1550 (mu 2); nonnumeric or > `minor_units` places → form error, nothing sent (`15.005` rejected, not rounded).
- R147 done — Pay form keeps its values after success.
- R148 done — Resubmitting unchanged pay form does not pay again (same key + same body → replay): balance falls once, one payment in the feed, no `pay-error`.
- R149 done — Changing any field makes the next submission a new payment (new key).
- R150 done — Form retries follow §7.

## Stage 2 — Activity feed (`/`)
- R151 done — `activity-list` children newest first in DOM.
- R152 done — `activity-item-{payment_id}` per visible payment with `data-visibility`.
- R153 done — `activity-parties-{id}` contains both handles.
- R154 done — `activity-amount-{id}` exactly formatted amount.
- R155 done — `activity-note-{id}` exactly the note, present even when empty.
- R156 done — `empty-activity` shown instead of the list when nothing is visible.

## Stage 2 — Requests screen (`/requests`)
- R157 done — `incoming-list`, `outgoing-list` containers.
- R158 done — `request-item-{id}` with `data-status`; `request-amount-{id}` exactly formatted.
- R159 done — `request-pay-{id}`, `request-decline-{id}` only on pending incoming; `request-cancel-{id}` only on pending outgoing.
- R160 done — `request-error` when pay/decline/cancel refused; `empty-requests` when both lists empty.

## Stage 2 — Split screen (`/split`)
- R161 done — `split-amount` (decimal, pay-amount rule), `split-handles` (comma-separated, in order), `split-note`, `split-submit`.
- R162 done — `split-preview` before submit with one `split-share-{handle}` per participant, exactly formatted; identical to server §9 shares.
- R163 done — `split-error` when the split is refused.

## Stage 2 — Refresh, competing clients, uncertain outcomes
- R164 done — After any successful action, balance, feed and request lists on the same page show new state without manual reload; refresh only after the write succeeds.
- R165 done — `wallet-refresh` on `/` refreshes balance and feed without clearing the pay form.
- R166 done — Latest refresh wins: a delayed earlier read never overwrites a later one, even out of order (also for available/held).
- R167 done — Refused payment: `pay-error`, refresh balance/feed, preserve all pay inputs.
- R168 done — Request cancelled elsewhere: pay refused → `request-error` and request list refreshed (stale pay button gone).
- R169 done — Lost payment response (incl. after commit): `pay-uncertain` with non-empty text, not `pay-error`; form stays retryable with same key and body.
- R170 done — Successful retry removes error/uncertain elements, refreshes balance and feed, money moves exactly once.
- R171 n/a — No background polling, live sync or recovery across page reloads required.

## Stage 2 — Upgrade from stage 1
- R172 done — Stage-2 import accepts this team's stage-1 export.
- R173 done — Browser signed in before export/import stays signed in afterwards (tokens preserved).
- R174 done — Imported pending requests stay payable through the request screen.
- R175 done — Payment lost before export is retryable after import with same key/body; UI recovers the original payment and refreshes imported balance; form and pending retry identity survive (no reload).

## Stage 2 — Authorizations model and invariants
- R176 done — Sum of wallet `total` always equals the seeded total; holds move no money.
- R177 done — `available = total − held` never negative; held funds cannot fund payments, authorizations or settlement net debits; captures may spend their own reservation.
- R178 done — Cumulative captures ≤ authorized amount; each idempotent capture moves money once; closed hold cannot be captured again.
- R179 done — `GET /me` keeps `balance` (= `total`), adds `total`, `available`, `held`; with no holds all agree, held 0.
- R180 done — `POST /payments` stays an immediate transfer (no hold, no capture).
- R181 done — Every 409 `insufficient_funds` (payments, request pay, settlements) evaluated against `available`.
- R182 done — Request pay stays immediate; splits unchanged.
- R183 done — Seven idempotent write paths (stage-1 five + `POST /authorizations` + `POST /authorizations/{id}/capture`), §7 rules independently.
- R184 done — Fixture `authorization_ttl_seconds` (default 600; positive integer else reset 422/400) applies to API-created authorizations.
- R185 done — Fixture `authorizations` array (default empty): id, from/to user ids, amount, note, visibility, status (`open`/`captured`/`voided`/`expired`), absolute `expires_at`.
- R186 done — Seeded `balance` is `total`; `available` derived by subtracting seeded open, unexpired holds.
- R187 done — Sum of a user's seeded unexpired open holds > balance → reset 422 `validation_failed`, state unchanged.
- R188 done — Only `open` holds anything; `expires_at` at or before now → `expired`, holds nothing; reflected lazily on every read and write (`GET /authorizations` shows expired, `GET /me` releases remainder).

## Stage 2 — Authorizations API
- R189 done — `POST /authorizations` {to_handle, amount, note?, visibility?} → 201 authorization body (authorization_id, from/to ids+handles, amount, captured_amount 0, remaining_amount, currency, note, visibility, status open, expires_at = created_at + ttl, payment_id null, payment_ids [], created_at).
- R190 done — Authorize errors: available < amount 409 `insufficient_funds`; amount range/integer 422; self 422 `self_payment`; note > 200 or bad visibility 422; unknown handle 404.
- R191 done — Open authorization is not a feed item.
- R192 done — `POST /authorizations/{id}/capture` {amount?, final?}: receiver only; amount defaults to remaining; `{}` vs `{"amount":N}` are different bodies for replay.
- R193 done — Capture → 201 payment (payment shape, `authorization_id` set, `request_id` null, amount captured, note/visibility copied, in feed by normal rule); other payments carry `authorization_id: null`.
- R194 done — Default final capture: status `captured`, `captured_amount`, `payment_id`, remainder released in the same step; second capture → 409 `authorization_not_open`.
- R195 done — `final: false` (boolean, default true): remainder stays held, status open; further captures up to remainder; capturing the entire remainder closes it.
- R196 done — `capture_exceeds_authorization` (422) compares with remaining; `captured_amount` cumulative; `payment_id` latest; `payment_ids` all captures in order; `remaining_amount` held now, 0 when closed.
- R197 done — Void/expiry close partially captured authorizations, release only the remainder, keep capture records.
- R198 done — New fields do not change idempotency body equality.
- R199 done — Capture errors: not open 409 `authorization_not_open`; expired 409 `authorization_expired`; amount > remaining 422 `capture_exceeds_authorization`; amount < 1 / non-integer 422; non-receiver 403; unknown 404.
- R200 done — `POST /authorizations/{id}/void`: payer only, no key; 200 voided, hold released; void of voided → 200; captured/expired → 409 `authorization_not_open`.
- R201 done — Capture/void by a non-permitted caller (incl. third parties) → 403.
- R202 done — `GET /authorizations` direction (outgoing payer / incoming receiver), status (expired-by-clock matches `expired`, never `open`), limit/offset/has_more as `/requests`; only caller's; newest first.

## Stage 2 — Authorizations UI
- R203 done — Route `/authorizations` (HTML for text/html, else JSON).
- R204 done — `wallet-balance` = formatted total (unchanged); `wallet-available` formatted available with `data-amount`, the headline number; `wallet-held` with `data-amount`, absent when held is zero.
- R205 done — Authorize form: `authorize-handle`, `authorize-amount`, `authorize-note`, `authorize-visibility`, `authorize-submit` (pay-form input rules); `authorize-error` when refused.
- R206 done — `authorization-list` children newest first; `authorization-item-{id}` with `data-status`; `authorization-amount-{id}` exact formatted.
- R207 done — `authorization-captured-{id}` only when status `captured`; `authorization-expires-{id}` text is RFC 3339 `expires_at`.
- R208 done — `authorization-capture-amount-{id}` (prefilled remaining) and `authorization-capture-{id}` only on incoming open; `authorization-void-{id}` only on outgoing open.
- R209 done — `authorization-error` when capture/void refused; `empty-authorizations` when the list is empty.
- R210 done — UI reflects seeded and new holds; available shown as spending balance immediately after reset with open holds.

## Stage 2 — Product quality
- R211 done — Coherent, calm finance look; consistent type, spacing, colour, controls; primary actions obvious.
- R212 done — Available/held/pending/loading/success/refused/uncertain states visually distinct.
- R213 done — People, amounts and timestamps formatted for people; technical ids only where helpful.
- R214 done — Usable at 375 px and desktop without horizontal scrolling.
- R215 done — Visible labels, apparent keyboard focus, sufficient contrast.
- R216 done — Considered empty, loading and error states.
- R217 done — All browser assets (fonts, scripts, styles) inside the image; nothing fetched from outside.

## Stage 2 — Concurrency
- R218 done — Concurrent requests serialize to some one-at-a-time order; invariants hold at every read.

## Added on coordinator review (msg 3b68e30d)
- R219 done — Stage-2 export/import round-trips authorizations (all statuses, captured_amount, payment_ids, expires_at), `authorization_ttl_seconds`, and authorize/capture idempotency records; replays after import return 200 with original bodies.
- R220 done — A stage-1 export (no authorizations/ttl) imports with empty authorizations, ttl 600, held 0.
- R221 done — Holds expire lazily by the clock, also right after import; `expires_at <= now` is expired; expiry releases the remainder exactly once (no double release on later capture/void).
- R222 done — Only `text/html` in Accept selects HTML on `/requests` and `/authorizations`; absent Accept, `application/json`, `*/*` get JSON; `/`, `/split`, `/signup`, `/login` served without a token (client-side gating).
- R223 done — Browser session token survives navigation between routes and a server-side import (same token strings).
- R224 done — UI lost-response retry re-sends the same Idempotency-Key and byte-equal body; a 200 replay is success.
- R225 done — Capture vs void vs expiry vs payment races never overspend (available never negative); each capture key moves money once; settlement net debit uses available.
- R226 done — `pay-visibility`/`authorize-visibility` default to public; UI amounts use the fixture's minor_units everywhere (JPY 0, BHD 3), incl. capture prefill and split preview.

## Stage-1 review findings fixed in stage 2
- M1 done — Every generated id (payment, request, split, settlement, authorization, user) never collides with any seeded or imported id.
- M2 done — Reset with 150 users with distinct passwords completes well under 10 s (target < 3 s): scrypt in parallel, per-record cost parameters stored with the hash.

## Stage 3 — Payment timestamps and seeds
- R227 done — Every payment's `created_at` is an RFC 3339 instant with offset (when it moved money), present on every endpoint returning a payment; `/activity` keeps ordering by it.
- R228 done — Seeded payments may supply `created_at`; omission uses reset time, before later API-created payments.
- R229 done — Seeded `created_at` in the future → reset 422 `validation_failed`, no state change.
- R230 done — Fixture `balance` stays the balance after all seeded payments; loading them does not change it.

## Stage 3 — `GET /me?as_of`
- R231 done — `as_of` optional RFC 3339 with offset; naive time, bare date, empty → 422.
- R232 done — Without temporal params `/me` keeps existing fields (no `as_of`), current corrected values.
- R233 done — With `as_of`: balance after every payment with effective time ≤ `as_of` (inclusive), before later ones.
- R234 done — `as_of` ≥ latest payment → current balance; before earliest → opening balance.
- R235 done — Response echoes `as_of` (and `known_at`) exactly as given.

## Stage 3 — `GET /statement`
- R236 done — `from`/`to` optional (default: wallet opening / now); `limit`/`offset` as `/requests`; invalid instants 422.
- R237 done — Payments the caller sent or received in half-open `[from, to)`, oldest first, each with `delta` and `balance_after`.
- R238 done — Ordering by (selected `effective_at`, payment id) ascending.
- R239 done — `opening_balance` = balance immediately before `from`; `closing_balance` = balance immediately before `to`.
- R240 done — opening + Σ deltas (full window) = closing; sent negative, received positive.
- R241 done — Pagination never changes `balance_after`, opening or closing; `has_more` correct incl. final partial page and offsets past the end.
- R242 done — Only the caller's own payments; feed visibility rules do not apply (no third-party public payments).
- R243 done — Statement contains money movements only (authorization, release, expiry are not entries); captures appear exactly once with their links.

## Stage 3 — Revisions and corrections
- R244 done — Every payment has a revision history; revision 1 = original amount, `effective_at = recorded_at = created_at`, `reason: ""`.
- R245 done — Opening balances = seeded ending balances − net effect of original seeded payments; corrections never change opening balances; new accounts open at 0.
- R246 done — `POST /payments/{id}/corrections` requires Idempotency-Key (§7 rules; eighth idempotent path) and the original sender; non-sender 403; unknown 404; no token 401.
- R247 done — Body fields all required: `expected_revision` positive integer; `amount` integer 0..1000000000; `reason` string 1..200; `effective_at` RFC 3339 not later than now; else 422 `validation_failed`.
- R248 done — Correction changes neither parties nor visibility; appends an immutable revision; 201 {payment_id, revision, amount, effective_at, recorded_at (server), reason}.
- R249 done — Recorded times for one payment strictly increase.
- R250 done — Stale `expected_revision` → 409 `stale_revision`.
- R251 done — Replay → 200 with the original revision even after newer revisions; different body same key → 409 `idempotency_key_reuse`.
- R252 done — Difference moves between the same two wallets atomically: increase debits sender, decrease debits receiver.
- R253 done — Currently unaffordable debit (against available) → 409 `insufficient_funds` (takes precedence).
- R254 done — Else any user's corrected total or available negative at any effective/event boundary (all movements at an instant combined, latest known revisions) → 409 `historical_overdraft`.
- R255 done — Either failure preserves balances, revisions, statements and idempotency state (key not claimed).
- R256 done — Sum of balances equals seeded total in every historical view.
- R257 done — Original payment and original idempotent responses unchanged; `/activity` shows the original payment; corrections are not feed payments.
- R258 done — `GET /payments/{id}/revisions` → `{"revisions": [...]}` in revision order incl. revision 1; only the two parties; third party 404 (even public); no token 401.
- R259 done — Settlement members' revision 1 uses committed_at for effective/recorded; corrections of settlement members or captures → 422 `linked_payment_immutable`.

## Stage 3 — known_at
- R260 done — `/me` and `/statement` accept `known_at` (RFC 3339 with offset; invalid/empty 422; echoed exactly).
- R261 done — Per payment select latest revision recorded at or before `known_at`; none → contributes nothing; omission = everything known when the read begins; then apply by effective time.
- R262 done — `as_of` inclusive and statement half-open retained; both instants may be in the future.
- R263 done — Statement entries add selected `revision`, `effective_at`, `recorded_at`; `payment.amount` = selected amount; zero-amount revisions appear with zero delta; never both a correction and the revision it replaces.
- R264 done — With no corrections and no `known_at`, previous behaviour unchanged.

## Stage 3 — Snapshots
- R265 done — Every first `GET /statement` returns an opaque `snapshot` token freezing selected revisions, window, balances, entries, default `to`.
- R266 done — `GET /statement?snapshot=T&limit&offset` pages the frozen result, unchanged by later payments, corrections or hold lifecycle events.
- R267 done — With `snapshot`, `from`/`to`/`known_at` → 422; unknown, another user's, or pre-reset token → 404; tokens last until reset (and import).
- R268 done — Concurrent corrections with the same expected revision cannot both succeed; snapshots unchanged during concurrent writes.

## Stage 3 — Historical holds
- R269 done — `/me?as_of=T&known_at=K`: balance = total, available = total − held, all in the same view.
- R270 done — Hold starts at creation; nonfinal capture reduces it at capture time; final capture, void or expiry releases the remainder at that event's time; expiry at `expires_at`.
- R271 done — Non-expiry events known at their server time; once creation is known the deadline is known; beyond now an open hold expires at its deadline; without `as_of` use request start.
- R272 done — Authorizations expose `closed_at` (null while open; event time when closed).
- R273 done — Seeded open holds are created at reset unless `created_at` supplied; seeded closed holds need no prior lifecycle.

## Stage 3 — Import
- R274 done — Stage-3 import accepts this team's stage-1 and stage-2 exports (and its own), accounting for authorizations and captures; tokens, retry records preserved.

## Stage 4 — Idempotent paths
- R275 done — Ten idempotent write paths: POST /payments, /requests, /requests/{id}/pay, /splits, /settlements,
  /authorizations, /authorizations/{id}/capture, /payments/{id}/corrections, /payments/{id}/refunds,
  /correction-batches; §7 rules (key required, replay 200 with the original body, reuse with another body 409).

## Stage 4 — Refunds
- R276 done — `POST /payments/{payment_id}/refunds`, body `{"amount": n}`, requires an Idempotency-Key; no token 401.
- R277 done — Unknown payment → 404 `not_found`.
- R278 done — Only the original receiver may refund; anyone else (sender or third party) → 403 `forbidden`.
- R279 done — Target may be a direct payment, a request payment, a capture or a settlement payment.
- R280 done — Target that is itself a refund → 422 `invalid_refund_target`.
- R281 done — Missing/non-integer/out-of-range amount (1..1000000000) → 422 `validation_failed`.
- R282 done — Σ refunds of a payment (including this one) > its current corrected amount → 422 `refund_exceeds_payment`.
- R283 done — A refund is a new payment receiver → original sender, `refund_of` = target id, `request_id: null`,
  `authorization_id: null`, `settlement_id: null`, note and visibility copied from the target.
- R284 done — Success → 201 with that payment; replay with the same key → 200 with the original body.
- R285 done — Moves existing money from the receiver's available funds (balance − held), else 409
  `insufficient_funds`; atomic (no balance, payment or key change on failure).
- R286 done — Refunds never reopen a request or an authorization and never restore a released hold.
- R287 done — Every other payment has `refund_of: null` (feed, statements, settlements, captures, imports).
- R288 done — Refunding a settlement payment never changes settlement membership (the refund has no settlement_id;
  the member keeps its own).
- R289 done — A refund is a payment like any other in history: revision 1 at created_at, statements, as_of, known_at,
  activity feed (visibility copied from the target).

## Stage 4 — Corrections with refunds
- R290 done — Single corrections remain available for ordinary direct and request payments (stage-3 rules).
- R291 done — Captures and refund payments cannot be corrected (single or batch): 422 `linked_payment_immutable`;
  settlement members cannot be corrected singly (stage-3 rule kept; batches only).
- R292 done — A correction cannot reduce a payment below its already-refunded amount: 422 `refund_exceeds_payment`.
- R293 done — Correction debits are checked against available funds (409 `insufficient_funds`).

## Stage 4 — Correction batches
- R294 done — `POST /correction-batches` needs a token (401) and a settlement operator (403 `forbidden`, same rules
  and order as settlements), and an Idempotency-Key.
- R295 done — `corrections`: 1..32 objects with distinct `payment_id`s, else 422 `validation_failed`.
- R296 done — Every item has the ordinary correction fields and validation (expected_revision, amount 0..1e9,
  reason 1..200, effective_at RFC 3339 not later than now) → 422 `validation_failed`.
- R297 done — Unknown payment in an item → 404 `not_found`.
- R298 done — Stale expected revision in an item → 409 `stale_revision`.
- R299 done — The operator may correct any ordinary, request or settlement payment (not only their own); captures
  and refunds → 422 `linked_payment_immutable`; below refunded amount → 422 `refund_exceeds_payment`.
- R300 done — Including any settlement member requires every member of that settlement, else 422
  `incomplete_settlement`.
- R301 done — Members of one settlement must have identical effective instants (offset spellings may differ), else
  422 `validation_failed`.
- R302 done — Ordinary single-payment corrections remain available for nonmembers.
- R303 done — Unknown fields (top level and items) are ignored.
- R304 done — Error precedence: item errors in input order; settlement completeness; resulting current available
  funds (409 `insufficient_funds`); historical total and available at every effective/event boundary (409
  `historical_overdraft`).
- R305 done — Affordability uses the combined effect of all proposed revisions (per user net).
- R306 done — A rejected batch leaves history, balances and idempotency records unchanged.
- R307 done — 201 `{correction_batch_id, recorded_at, revisions}` with revisions in input order.
- R308 done — All new revisions share recorded_at, strictly later than every member's previous recorded_at; each
  revision exposes `correction_batch_id`.
- R309 done — Original payments, receipts and original payment/settlement retries keep their original bodies.
- R310 done — New statements reflect the new revisions; earlier snapshot tokens page their frozen entries.
- R311 done — Replay → 200 with the original batch response.
- R312 done — Concurrent corrections (single or batch) sharing any expected payment revision cannot both succeed.

## Stage 4 — Import
- R313 done — Accepts this team's stage-1, stage-2 and stage-3 exports (and its own), retaining settlement membership,
  corrections, snapshots, holds; refunds and batch ids survive a stage-4 round trip.

## Stage-2 review findings fixed in stage 3
- M3 done — `/authorizations` does not label captured/voided holds "Expired <date>".
- M4 done — At 375 px the activity feed is not ~1400 px below the forms; all data-testids stay in the DOM.

## Stage 3 evidence map
- `test_service.py`: `Seeds` (seeded created_at kept verbatim, future → 422 unchanged, opening via as_of), `AsOf`
  (inclusive boundary, future, echo, invalid forms incl. naive/date/empty), `Statement` (order, half-open window,
  opening/closing, pagination invariance, final page and offset past end, third-party public excluded, snapshots
  frozen across payments and corrections, 422 mixing, 404 other user/unknown/after reset), `Corrections`
  (increase/decrease, revisions endpoint, original feed, zero amount, replay of revision 2 after revision 3, reuse,
  stale, known_at selection, effective time moving windows, every validation row, 401/403/404, insufficient_funds
  before historical_overdraft with nothing changed and the key unclaimed, held funds, combined same-instant
  boundary, settlement/capture immutable, 20 concurrent corrections on one revision → one 201),
  `HistoricalHolds` (creation, nonfinal capture, void, known_at before void runs to deadline, expiry at expires_at,
  closed_at, statement has the capture once and no hold events), `Stage3Import` (real stage-1 and stage-2 servers'
  exports, stage-3 round trip with revisions and snapshots).
- `test_ui.py`: carried stage-2 browser tests plus M3 and M4.

## Stage 3 interpretation decisions
- Correction check order: 404 → 403 → 422 linked_payment_immutable → 422 validation → 409 stale_revision →
  409 insufficient_funds (current available of the debited party) → 409 historical_overdraft (both parties' total
  and available at every effective/event boundary under the latest revisions, all movements at one instant combined).
- Corrections of request payments are allowed; only settlement members and captures are linked.
- `from` later than `to` in a statement is 422 `validation_failed` (the window would be negative).
- Statements echo `from` (or null), the effective `to`, and `known_at` when given; snapshot responses repeat the token.
- Default `to` and the default `as_of` for known_at-only reads are the request start (never before the last recorded
  instant).
- Seeded closed holds have no lifecycle (hold nothing in any historical view); their `closed_at` is `expires_at` when
  expired, else reset time.
- Import of a stage-1/2 export (no lifecycle recorded), after review B1 and rulings D2–D4: every hold is rebuilt from
  `created_at`, its capture payments and its close — captured: the last capture; voided: the latest known event
  (the void time is not in the export); expired: `expires_at`; open: still open. A hold whose close or deadline is not
  after its creation contributes nothing and held is never negative (covers seeded captured/voided holds and D3).
  A hold marked `expired` whose `expires_at` is still in the future at import cannot have expired by the clock, so it
  was seeded expired and contributes nothing (D2). Captured amounts not explained by capture payments after
  creation were captured before creation (seeded) and reduce the initial hold.
- Snapshot tokens survive export/import; reset clears them.
- An unencoded `+` in an instant query value (arrives as a space) is accepted as `+`.

## Stage 2 evidence map
- API, holds, expiry, races, M1, M2, stage-1/stage-2 import: `test_service.py` classes `Authorizations`, `SeededAuthorizations`,
  `Stage1Import` (starts the real stage-1 server and imports its export), `Stage2RoundTrip`, `Html` (Accept negotiation).
- Browser: `test_ui.py` — decimal parsing, JPY/BHD formatting, double submit, lost response after/before commit,
  refused payment keeps inputs, latest refresh wins out of order, request cancelled elsewhere, split preview (BHD),
  holds flow (seeded hold, capture prefill, partial capture), void + capture refused, session and pending retry
  surviving export/import, 375 px with no horizontal scroll, labelled inputs and no placeholder text leaks.

## Stage 2 interpretation decisions
- The authorize form appears on `/` and on `/authorizations`; the wallet summary (available headline, total, held)
  appears on `/`, `/requests` and `/authorizations`; `wallet-refresh` is on `/`.
- `incoming-list`, `outgoing-list` and `authorization-list` are always rendered (possibly empty) next to the empty
  state; `activity-list` is replaced by `empty-activity` when nothing is visible.
- Capture of a hold whose status is `expired` (by clock or seeded) is 409 `authorization_expired`; void of an
  expired hold is 409 `authorization_not_open`. `final` of the wrong JSON type is 400 `malformed_request`.
- An empty capture body is treated as `{}` (optional body, as for request pay).
- Lost outcomes on request pay, hold, split and capture show a neutral "couldn't confirm" notice (`*-uncertain`),
  not the refusal element; the retry reuses the same key and body.
- Passwords hashed by this version use scrypt N=2^12 (r=8), stored with the hash; stage-1 imports keep N=2^14.

## Stage 4 evidence map
- `test_service.py` classes `Refunds` (R276–R289, concurrent refunds), `CorrectionsWithRefunds` (R290–R293),
  `CorrectionBatches` (R294–R312: shape, replay, snapshots, known_at, every validation row, input-order precedence,
  completeness, offset spellings, combined affordability, historical overdraft with atomic rejection and unclaimed
  key, concurrent batches and single corrections on one revision), `Stage4Import` (R313: own round trip; a real
  stage-3 export with corrections, settlement and snapshot); carried `Stage1Import`/`Stage3Import` cover stage-1/2.

## Stage 4 interpretation decisions
- Refund check order: 404 → 403 → 422 `invalid_refund_target` → 422 `validation_failed` → 422
  `refund_exceeds_payment` → 409 `insufficient_funds`.
- Single correction order (stage 3 plus R292): 404 → 403 → 422 `linked_payment_immutable` → 422 validation → 409
  `stale_revision` → 422 `refund_exceeds_payment` → 409 `insufficient_funds` → 409 `historical_overdraft`.
- Batch item order (per item, items in input order): 404 → 422 `linked_payment_immutable` → 422 validation → 409
  `stale_revision` → 422 `refund_exceeds_payment`; then completeness → identical member instants → funds → history.
- Every revision exposes `correction_batch_id` (null for revision 1 and single corrections).

## Serialization point
One process, one in-memory store, one `threading.Lock` held for the full duration of every state read-modify-write
(and every snapshot read). That lock is the single serialization point; see `RUN.md`.
Each write takes one instant inside the lock (`State.begin_op`): clock expiry, every validation against the clock
(capture/void of a hold at or past `expires_at`, a correction's `effective_at` ≤ now) and the recorded instant all use
it, so no event can be recorded at or after a deadline it was validated before (review B2).
