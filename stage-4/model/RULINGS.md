# Stage 4 — modeler rulings (stage-1 R1–R30, stage-2 R31–R52, stage-3 R53–R80 carried; stage-4 R81–R100)

Every place where the stage-1 requirements leave a choice open, the reading the model takes, and the sentence
it rests on. "Accept set" means the model accepts any of the listed (status, code) answers; every
alternative changes nothing, so the ambiguity never affects state.

## Error precedence

R1. **Pre-checks on the five idempotent writes are unordered.** Missing/unknown token (401), body that does not
parse as a JSON object (400 `malformed_request`), absent/empty key (400 `missing_idempotency_key`), key over 255
chars (422) and, for settlements, authenticated non-operator (403) form one accept set.
Rests on: "After the body has parsed as a JSON object and the caller is authenticated, an already claimed key
is resolved before endpoint field validation" — it orders these before key resolution, not among themselves.

R2. **A claimed key is resolved before field validation and resource checks**: same canonical body → 200 with
the stored response; different → 409 `idempotency_key_reuse`. Rests on §7 last paragraph.

R3. **Field errors and resource errors (404 unknown handle, 422 `self_payment`/`self_request`, 403, 409
`request_not_pending`) form one accept set; `insufficient_funds` is always last.** The text gives no order
between field and resource errors; funds are checked only when the operation could otherwise run.

R4. **Settlement entries**: shape errors (missing `transfers`, not a list → 422 or 400, 0 or >32 entries → 422)
first; then entries in input order, the first entry with any error decides (its own errors form an accept set);
then collective funds. Rests on "Entry errors take precedence in input order, before insufficient funds" and
"malformed batch shape is 422". An entry that is not an object, or a handle of the wrong JSON type inside an
entry, accepts 400 or 422 (shape rule vs. §5 wrong-type rule).

## Fields and types

R5. `amount`: JSON number with integral value in 1..1000000000 (`1e3`, `1000.0` valid); anything else including
strings, booleans, null, missing → 422. NaN/Infinity tokens are unparseable → 400. (§4, §5.)

R6. `note`: optional, default `""`; non-string (incl. null) or more than 200 **Unicode code points** → 422.
The text says "200 characters"; code points is the reading taken. The driver only probes the 200/201 boundary
with ASCII so UTF-16-counting products are not flagged.

R7. `visibility`: optional, default `public`; anything else present (incl. null, `"PUBLIC"`) → 422.

R8. Handle fields (`to_handle`, `payer_handle`): missing → 422; non-string → 400 (§5 "Other wrong JSON types");
null accepts 400 or 422. A string that cannot be a handle (fails `^[a-z0-9_]{1,20}$`, e.g. `"BOB"`) accepts
404 or 422; a well-formed unknown handle is 404.

R9. `participant_handles`: missing or empty → 422; not a list → 400 (null: 400 or 422); a non-string element
→ 400 or 422; duplicate → 422; unknown → 404 (accept set across all). A share of 0 still creates a request
(§9), so requests of amount 0 exist and paying one moves 0.

R10. Query integers: plain decimal digits only (`007` is 7 and valid; empty, `+4`, `4.0`, `1e2` → 422).
A repeated parameter uses its last value (the driver never repeats one).

R11. Bodies whose top level is not a JSON object (array, string, null, empty body) → 400 `malformed_request`.

## Identity, auth

R12. Seeded users, payments and requests keep their fixture ids in API responses (the fixture example and the
`/me` example both show `u_ada`).

R13. Signup email form: exactly one `@` with non-empty parts on both sides. Derived handle per §4. When the email
is registered and therefore its handle is also taken, either `email_taken` or `handle_taken` is accepted.
Validation (422) and conflicts (409) on one signup form an accept set. `display_name` is any string.

R14. Login with a missing field accepts 422 or 401; wrong type → 400; unknown email or wrong password → 401.

R15. Authorization must be exactly `Bearer <token>`; anything else → 401.

## Idempotency

R16. Key scope is (authenticated user, method, path, key). Same key on a different path is an independent
request whatever the body. Rests on "The same key with the same body on a different path is a different
request, not a replay". The driver only probes the different-path case with the same body.

R17. Only a 201 claims a key; any 4xx leaves it reusable (§7 table). "Same body" = equal JSON value after
parsing; the driver never probes `1000` vs `1000.0` as same/different.

R18. A replay is checked twice: against the model, and byte-for-value against the product's own original 201
body.

## Requests, payments, splits

R19. A payment created by paying a request carries a note the text does not fix (model: any string; it copies
the request note). A request created by a split likewise has an unfixed note.

R20. Decline/cancel by the other party of the request → 403; by a non-party → 403 or 404 ("not visible to this
caller"). Same for pay. Combined with "not pending", either error is accepted.

R21. Decline/cancel ignore any body; no key is used.

R22. Seeded requests have `payment_id: null` whatever their status; seeded payments have `request_id` and
`settlement_id` null and are older than every payment created through the API.

## Lists

R23. Newest first by `created_at`; equal timestamps have unspecified order. The driver therefore compares
item count, `has_more`, membership in the model's filtered set, item content, no duplicates, and that the
product's own `created_at` values never increase down the page. It does not demand a particular tie order.

## Time

R24. RFC 3339 with explicit offset: `±hh:mm` or `Z` (RFC 3339 defines `Z` as the zero offset). Fractional
seconds allowed. *(Stage 3 supersedes "not compared": see R53.)*

## Settlements

R25. Members' `created_at` equals `committed_at`, and every member's `settlement_id` equals the batch id;
non-member payments expose `settlement_id: null` (§11, so every payment object carries the field).

R26. Affordability is per wallet on net movement (balance + in − out ≥ 0); a wallet may go "through" zero
inside the batch.

R27. Operators see only ordinary feed visibility and may move money between any wallets, including their own.

## Reset, export, import

R28. A fixture with a negative balance → 422, state unchanged (checked by the next step's invariant sweep). Other
malformed fixtures accept 400 or 422 (the driver does not send them).

R29. Export → import of the product's own export is compared against the model restoring its own snapshot;
tokens issued after the export must be rejected after import (401); tokens from before stay valid; replays
of keys claimed before the export return the original response.

R30. Import with wrong track, `format_version` ≠ 1, missing state, or `state: {}` → 422; invalid JSON → 400.
`state: {}` being invalid is the reading taken (an empty object holds no currency, users or totals).

---

# Stage 2 additions

## Holds and funds

R31. `held(user)` = sum of `remaining_amount` over the user's **open** authorizations as payer;
`available = total − held`; `balance == total`. Every stage-1 `insufficient_funds` (payments, request pay,
settlement net debits) and the new authorization check compare with `available` (stage-2 "Held funds cannot
fund new payments, authorizations or settlement net debits").

R32. A capture moves money from the payer's wallet without an `available` check: the money it spends is the
hold's own reservation ("Captures may spend the money reserved for them").

## Capture

R33. `amount` optional (default: remaining). Present: a JSON number with integral value ≥ 1 (`1500.0` valid);
otherwise 422 `validation_failed`. Above the remaining amount → 422 `capture_exceeds_authorization`; when it is
also above 1000000000 either that or 422 `validation_failed` is accepted.

R34. `final` optional, default `true`; a non-boolean (e.g. `"no"`) accepts 400 `malformed_request` (§5 wrong
type) or 422.

R35. Capture errors (404 unknown, 403 not the receiver incl. non-parties, 409 not open, 422 exceeds, 422 field
errors) form one accept set; the text gives no order. Unknown id is 404 for every caller ("Unknown
authorisation | 404").

R36. **An authorization that has expired (by clock or seeded status `expired`) is `expired`**, so a capture of it
accepts either 409 `authorization_expired` or 409 `authorization_not_open` (the table lists both conditions and
an expired one satisfies both).

R37. After a capture: `captured_amount += amount`, `payment_id` = this capture, `payment_ids` appends it,
`remaining_amount -= amount`. If `final` (default) or remaining reaches 0: `status = captured`,
`remaining_amount = 0` (uncaptured remainder released in the same step).

R38. The capture payment: `from` payer → `to` receiver, `note`/`visibility` copied from the authorization,
`authorization_id` set, `request_id` and `settlement_id` null. Every payment carries `authorization_id`
(null when not a capture).

## Void

R39. Only the payer; other party or non-party → 403 (explicit in the text). `open` → `voided`, remaining
released, capture records kept; `voided` → 200 current state; `captured`/`expired` → 409
`authorization_not_open`. 403 and 409 together form an accept set. No key; body ignored.

## Expiry and time

R40. An authorization is expired at time t iff it is open and `expires_at ≤ t`. The model expires holds only
when told: the driver brackets every call by its send/receive time widened by 1 s (GUARD) — deadlines before
the bracket are expired, deadlines inside it may or may not be, and every prefix (in deadline order) is tried.
Inside bursts, uncertain expiries are extra events placed anywhere in the linearization.

R41. New authorization: `expires_at − created_at == authorization_ttl_seconds` exactly (both read from the
product response). The model then adopts the product's `expires_at` as the deadline.

R42. Seeded `expires_at` is compared as an instant (format may differ). Seeded `open` with `expires_at ≤
reset time` is `expired` from the start and holds nothing.

R43. Seeded closed authorizations (`captured`, `voided`, `expired`): the fixture gives no `captured_amount`; any
integer is accepted. They have `payment_id: null`, `payment_ids: []` and `remaining_amount: 0` (no capture
records exist). Seeded open ones start with `captured_amount: 0`, `remaining_amount: amount`. Seeded
authorizations are older than every API-created one; their `created_at` is format-checked only.

## Fixture

R44. `authorization_ttl_seconds`: omitted → 600; an integer ≥ 1 is valid; `0`, negative or non-integral number →
422; a non-number (`"600"`) accepts 400 or 422. Seeded open unexpired holds summing above the payer's balance
→ 422, state unchanged. Omitted `authorizations` = []. A seeded authorization amount outside 1..1000000000 accepts 400 or 422 (the
driver never sends one).

## Lists

R45. `GET /authorizations`: payer = `outgoing`, receiver = `incoming`; `status` ∈ open/captured/voided/expired,
anything else (incl. `pending`, `OPEN`) → 422; paging as R10/R23. Clock-expired items show `expired` and match
only `status=expired`.

R46. Open authorizations never appear in `/activity`; capture payments do, by the ordinary rule.

## Idempotency

R47. Seven idempotent paths; `POST /authorizations/{id}/capture` keys are scoped per path like `/pay`. A capture
replay returns the original payment even after the authorization closed or expired.

## Upgrade

R48. A stage-2 service must import its own stage-1 export (204). The driver checks this by driving a stage-1
service for the first third of a seed, exporting there and importing into the stage-2 service; afterwards all
stage-1 sessions, receipts, pending requests and key replays must behave as if nothing happened, with
`total = balance`, `held = 0`, `available = balance`, TTL 600 and no authorizations.

R49. A stage-1 replay after the upgrade returns the stage-1 original body unchanged (no `authorization_id`
added): "A successful replay returns the original response".

R50. Responses for stage-2 entities may carry extra fields; every field named in the text must be present.

R51. `GET /me` with no open holds: `held = 0`, `available = total = balance`.

R52. UI routes (`Accept: text/html`) are not modelled; the driver never sends that header, so `/requests` and
`/authorizations` must answer JSON.

---

# Stage 3 additions

## Instants

R53. **Every instant is compared as an instant, never as a string.** `2026-09-24T13:20:00+00:00`,
`…13:20:00Z` and `…18:50:00+05:30` are equal. An instant the product assigns (payment `created_at`, settlement
`committed_at`, correction `recorded_at`, authorization `created_at` and a void's `closed_at`, the reset time of
seeded rows without `created_at`) must lie inside the call's send/receive window ± 1 s. The model then adopts
it, and every later read must reproduce it exactly. Rests on: "Every payment's `created_at` is an RFC 3339
instant with an offset identifying when it moved money."

R54. Query and body instants are accepted only in the strict RFC 3339 form `YYYY-MM-DDThh:mm:ss[.frac](Z|±hh:mm)`
with a real calendar date. A naive time, a bare date, an empty value, `+0000`, epoch digits or `25:00` give
422. The driver never sends lowercase `t`/`z`, a space separator or more than 6 fractional digits, because the
text leaves them open. Rests on: "Anything else — a naive local time, a bare date, an empty value — is 422".

R55. `as_of` and `known_at` are echoed **byte for byte** ("exactly as given", "Echo supplied `known_at`
exactly"). The driver URL-encodes `+` as `%2B`.

## Seeded history

R56. A seeded payment without `created_at` gets the reset time. All such payments share an instant inside
the reset call's window, which the driver reads back from `GET /activity`. Seeded open holds without
`created_at` get the same reset time ("Seeded open holds are assumed created at reset unless `created_at` is
supplied"). A supplied `created_at` on a seeded hold is both its creation and its known-at instant, matching
seeded payments ("A seeded payment's supplied `created_at` is also its original recorded/effective time").

R57. Opening balance = seeded `balance` − net effect of the seeded payments; signup wallets open at 0. The
generator only produces consistent, nonnegative seeded history ("Seeded history is consistent and
nonnegative"), so a fixture whose opening balance would be negative is never sent.

R58. Seeded **closed** holds (`captured`, `voided`, `expired`) hold nothing in any historical view, and their
`closed_at` may be any instant or null ("seeded closed holds need not reconstruct a prior lifecycle"). A seeded
`open` hold whose `expires_at` has already passed is `expired` with `closed_at = expires_at`, and it holds funds
only in views with `created ≤ T < expires_at`.

R59. A seeded `created_at` later than the reset instant → 422, state unchanged. A non-RFC-3339 value → 422 or
400.

## Historical reads

R60. View `(T, K)`: for each payment, select the latest revision with `recorded_at ≤ K`. K absent = every
revision. A payment with no selected revision contributes nothing. `total(T, K)` = opening + Σ signed
selected amounts with `effective_at ≤ T` (inclusive). T absent = no limit, which is the instant of the read,
because no effective time can be later than now.

R61. Held funds in view `(T, K)`. A hold contributes if its creation instant `c ≤ min(T, K)`. Its
capture/void events count if their instant is ≤ min(T, K). A nonfinal capture reduces the hold. A final
capture, a capture of the whole remainder, or a void releases it. Expiry releases it when `expires_at ≤ T`,
whatever K is ("Once creation is known, the expiry deadline is known too"). With T absent, expiry follows
the hold's current clock state. A capture's event instant is the capture payment's `created_at`.
`closed_at` of a captured hold must equal that instant, so money and hold move together at the boundary
(R73).

R62. `GET /me` with `as_of` and/or `known_at` sets `balance = total`, `held` and `available = total − held` from
the same view. Without either parameter, the stage-2 fields are unchanged (current values, corrected).
`as_of`/`known_at` on `/statement` and every other unknown parameter are ignored (stage-1 general rule).

## Statements

R63. `from` absent = before everything (the opening balance applies before any movement, including
corrections dated earlier than the wallet's first payment). `to` absent = the instant the read began. Both
are frozen into the snapshot. Window `[from, to)` on the **selected effective** instant.

R64. Ties in `effective_at` are ordered by payment id ascending as plain string comparison of the product's
ids. `balance_after` is the running balance in that order. Opening = balance before `from`, closing = balance
before `to`, and both describe the full window whatever `limit`/`offset` say.

R65. `entry.payment` is the ordinary payment object with `amount` replaced by the selected revision's amount.
Every other field is the original. `revision`, `effective_at` and `recorded_at` sit beside `delta` and
`balance_after`.

R66. **`from` later than `to`** (explicit, or `from` later than the default now): the text does not say. Both
200 (empty window) and 422 `validation_failed` are accepted. On 422, no snapshot exists. When the driver
generates both bounds it orders them, so this case comes only from a `from` in the future.

R67. Snapshot paging: limit/offset rules as `GET /requests`. A first response carries `snapshot`; later pages
need not. `from`/`to`/`known_at` alongside `snapshot` → 422. Unknown token, another user's token or a token
from before a reset → 404. When both apply, either is accepted. An empty `snapshot=` value → 404 or 422. Several
first reads may share one product token. Tokens issued before an import are never probed, because the text
says only "Tokens last until reset".

## Corrections

R68. Pre-checks follow stage 1's idempotent writes (R1, R2). The key scope includes the payment id in the path.
After that, these form one accept set: unknown payment 404, authenticated non-sender (receiver or third
party) 403, field errors 422, `linked_payment_immutable` 422 and `stale_revision` 409. `insufficient_funds` and
then `historical_overdraft` are evaluated only on a request with none of those errors.

R69. Fields: `expected_revision` is a JSON integer ≥ 1 (a string or float → 422 or 400). `amount` follows R5
with a minimum of 0. `reason` is a string of 1..200 code points. `effective_at` is a strict instant (R54) ≤ the
request instant. All four are required; a non-string `reason`/`effective_at` → 422 or 400.

R70. `diff = amount − current amount`. A positive diff debits the sender; a negative one debits the receiver.
The debtor's current **available** must cover |diff|, or the result is 409 `insufficient_funds` (the stage-2
rule that every insufficient-funds check uses available). Otherwise the new revision is applied
tentatively. If either party's total **or available** is negative at any boundary instant — the effective
instants of every party payment's latest revision, plus the creation, capture, void and expiry instants of
the party's holds, each boundary including every movement at that instant — the result is 409
`historical_overdraft` and nothing changes. A zero diff with a new effective time is still a correction and
is still history-checked.

R71. The 201 body is `{payment_id, revision, amount, effective_at, recorded_at, reason}`. `effective_at`
is compared as an instant. `recorded_at` must be strictly later than the previous revision's. A replay
returns that body with 200, even after newer revisions. The same shape is used for each element of
`GET /payments/{id}/revisions`, with revision 1 having `reason: ""` and `effective_at = recorded_at =
created_at`.

R72. Payments with a non-null `settlement_id` or `authorization_id` are linked and immutable. Request
payments and seeded payments are correctable. Feed items, payment receipts and every stored idempotent
response keep the original amount.

R73. Captures: the payment's `created_at` is the capture event instant for both the money and the hold. A
captured hold's `closed_at` equals that instant. A void's `closed_at` is the void call's instant. An expired
hold's `closed_at` equals `expires_at`.

R74. Revisions: parties only. A third party → 404 even for a public payment; unknown → 404; no token → 401.

## Upgrades

R75. A stage-3 service must import its own stage-1 and stage-2 exports (204). The driver runs a chain
(1→3, 2→3 or 1→2→3) over the first part of a seed. After the upgrade, every payment's revision 1 is its
original amount at its original `created_at`. Settlement members and captures stay immutable. Holds keep
their creation and capture instants.

R76. Holds closed on stage 2 keep their history after the upgrade: they hold funds from creation, nonfinal
captures reduce them at their capture instants, and they hold nothing after the closing event. A final
capture closes at its payment's `created_at` and an expiry at `expires_at`; both are exact. **Voids issued on
stage 2** follow the coordinator ruling of 2026-10-04 (msg e4152b91): a stage-2 export records no void
instant and stage 2 is frozen, so the stage-3 `closed_at` may be any instant from the hold's latest known
event (its creation or last capture) to the end of the import call. The model adopts the product's value,
and every later historical read must be consistent with that value. A `closed_at` before the latest known
event, after the import, or null is a divergence. *(History: strict void-call window in 2a02cfe/e5e63f8,
relaxed for voids only by that ruling.)*
Deterministic check: every chain run that passes through stage 2 sets the TTL to 3 s. It authorizes three
holds and closes them by final capture, void and expiry. 1→2→3 chains skip the expiry case: a stage-1 export carries no TTL, so it is 600 s after the import. After the upgrade it reads `/me?as_of=created+1µs`
for each hold and runs the time-travel property there.

## Concurrency and snapshots

R77a. Snapshot stability (scenario `snapstab`, reviewer finding F3): a first statement read with limit 200,
then a correction of one of its payments, a payment, a capture, a void and another correction, then the
snapshot paged one entry at a time and in full. Every page is compared field by field with the frozen
first read: `payment.amount`, `revision`, `effective_at`, `recorded_at`, `delta`, `balance_after`,
opening/closing and `has_more`.

R77. Corrections racing on one `expected_revision` are linearized like every other burst, so at most one
succeeds and the others give `stale_revision`. Snapshot pages read inside a burst must equal the frozen
result whatever position the linearization gives them.

R78. The driver issues first statement reads and temporal `/me` reads only outside bursts. With concurrent
writes, the instant a read "began" is not observable.

## Time-travel property (driver `travel` step)

R79. For each view `(T, K)` drawn from event instants ± 0 / 1 µs / 1 ms / 1 s / 1 h, far past and far future,
and for every user, the driver checks:
- `GET /me?as_of=T&known_at=K` has `balance = total`, `available = total − held`, and all of them ≥ 0;
- `GET /statement?to=T+1µs&known_at=K` closes at that `total` and opens at `GET /me?as_of=1900-01-01Z` (the
  opening balance);
- `balance_after` chains through every entry;
- the totals of all wallets sum to the seeded total.

Every one of these responses is also compared with the model.

R80. UI routes are still not modelled (R52).

---

# Stage 4 additions

## Refunds

R81. `POST /payments/{id}/refunds` is the eighth idempotent path; the key scope includes the payment id. The
following form one accept set: unknown payment 404, caller not the original receiver 403 (sender or third
party), invalid amount 422 (R5 with minimum 1), a refund as the target 422 `invalid_refund_target`, and
cumulative refunds + amount > the latest revision's amount 422 `refund_exceeds_payment`.
`insufficient_funds` (409) is checked last, against the refunder's **available** funds. Rests on: "It moves
existing money from the receiver's available funds, or fails 409 insufficient_funds, atomically."

R82. A refund is a new payment from the original receiver to the original sender, dated now (revision 1 at its
`created_at`), with `refund_of` = target, `request_id`/`authorization_id`/`settlement_id` null, and the
original note and visibility. It appears in the feed, the statements and the revisions like any payment. It
never changes requests, holds, settlements or the target's revisions. Every other payment shows
`refund_of: null` from stage 4 on.

R83. Corrections and refunds:
- captures and refunds cannot be corrected (422 `linked_payment_immutable`);
- a correction below the refunded total is 422 `refund_exceeds_payment`;
- both are pooled with the other item errors (R68);
- correction debits are checked against available funds (unchanged from R70).

## Correction batches

R84. `POST /correction-batches` is the tenth idempotent path. Its pre-checks are the settlement ones: 401,
non-operator 403, missing key 400, non-object body 400. A missing or non-list `corrections`, an empty list or
more than 32 items → 422 (400 also accepted for a non-list). Items are checked in input order, and the first
bad item decides the response: its field errors, 404, `stale_revision`, `linked_payment_immutable` (captures
and refunds only) and `refund_exceeds_payment` form that item's accept set. Duplicate payment ids are a
list-level 422; the first item error is also accepted, because the text does not order the two rules.

R85. Then, per settlement touched by the batch, missing members → 422 `incomplete_settlement` and unequal
effective instants → 422 `validation_failed`, compared as instants (R53). If both apply, either is accepted.
Next, the combined current available check: every user's available after all proposed revisions must be ≥ 0,
else 409 `insufficient_funds`. Last comes the historical check for every affected user (R70) → 409
`historical_overdraft`. A rejection changes nothing: no revision, balance or idempotency record.

R86. 201 body `{correction_batch_id, recorded_at, revisions}`, with revisions in input order. Each revision has
the R71 shape plus `correction_batch_id`. Every revision's `recorded_at` equals the batch `recorded_at`, which
lies inside the call window and is strictly later than each member's previous revision. The revision list of
`GET /payments/{id}/revisions` carries `correction_batch_id` on batch revisions. On other revisions the field
may be absent or null.

R87. Single corrections of settlement members stay 422 `linked_payment_immutable`. Batches may correct
ordinary, request and settlement payments, whether or not the operator is a party.

## Snapshots, imports and upgrades

R88. Snapshot tokens survive a stage-4 service's own export/import and a stage-3 → stage-4 upgrade ("retaining
settlement membership, corrections and snapshots"). Tokens issued before the export keep paging their frozen
entries afterwards. Tokens issued after the export are never probed. Entries frozen in stage 3 have no
`refund_of`; an extra `refund_of: null` on them is allowed.

R89. Upgrade chains: any subset of 1 → 2 → 3 → 4 (`--stage1-base`, `--stage2-base`, `--stage3-base`). R75/R76
apply at every step. After each upgrade to stage 3 or 4, the lifecycle check is re-run.

## Gaps from the stage-3 review

R90 (F10). Every plain run, with no earlier-stage base, starts with a deterministic round trip:
1. holds closed by final capture and void, and by expiry when the TTL is short (half of plain runs seed TTL 3 s);
2. the service's own export, then import;
3. `/me?as_of=created+1µs` and the time-travel property for each hold.

The stage-4 service's own export must keep exact void instants.

R91 (F11). No capture or void may be recorded at or after the hold's `expires_at`: a successful capture's
payment `created_at` < `expires_at`, and a successful void's `closed_at` < `expires_at`. This is checked on
every capture and void. The `deadline` scenario (short TTL) fires three nonfinal captures, a final capture, a
void and a payer payment at fixed offsets from −80 ms to +40 ms around the deadline. The burst is then
linearized, with expiry as an extra event.

## Deterministic stage-4 scenarios

R92. `refund_rules`:
1. a partial refund;
2. a correction below the refunded total (422);
3. a refund of more than the remainder (422);
4. a hold of all the refunder's available funds, then a refund of 1 (409 `insufficient_funds`);
5. a refund of the refund (422 `invalid_refund_target`);
6. release of the hold.

`settle_batch`: a fresh two-member settlement, then:
1. a batch missing a member (422 `incomplete_settlement`);
2. a batch with unequal instants (422);
3. a single correction of a member (422 linked);
4. a complete batch whose members spell one instant with different offsets;
5. a refund of a member;
6. a replay of the batch (200, identical body).

Burst `batch race`: two batches and a single correction sharing one expected revision, plus a refund and
a payment. At most one revision per expected revision may succeed.
