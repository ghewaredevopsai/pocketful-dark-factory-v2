# Stage 2 — modeler rulings (stage-1 rulings R1–R30 carried unchanged, stage-2 rulings R31–R52)

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
seconds allowed. Values are not compared with the model.

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
