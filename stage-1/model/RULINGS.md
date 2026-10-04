# Stage 1 — modeler rulings

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
