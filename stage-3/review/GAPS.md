# Stage 3 review — gaps in the supplied checks

Revision reviewed: `d21365f0e437e4165ba4b4b5af45256cdfadd00e`. The supplied stage-3 checks are 6 tests:
a payment carries an instant, `/me` unchanged without `as_of`, future `as_of`, statement arithmetic, balance walk,
and one correction changing the balance. They leave out everything below; what covers each gap here:

| Gap in supplied checks | Covered by |
|---|---|
| Seeded `created_at` kept verbatim; future → 422 unchanged; opening balance via `as_of` before history | `probes3.py::p_seeds` |
| `as_of` inclusive at an instant, ±1 µs, before history, far future; sum = seeded total in every view; echo verbatim with `Z` and `+05:30`; 7 invalid forms; signup wallet opens at 0 | `p_as_of` |
| Statement order with an equal-instant tie (p_2 < p_3), own payments only (public ones of others excluded), signed deltas, running `balance_after`, half-open window at both ends, opening/closing = `/me as_of` 1 µs before, empty window, future `to`, invalid params, 401 | `p_statement` |
| Pagination invariance with limit 1, the final partial page, offset past end, `has_more` | `p_statement` |
| Snapshot frozen across a payment, a correction, a nonfinal capture and a void; balances frozen; 422 with from/to/known_at; unknown params ignored; 404 for another user / unknown / pre-reset token; survives import; identical across 30 reads during 60 concurrent payments | `p_snapshot` |
| Correction validation matrix (21 rows), 401/400/403 receiver/403 third party/404, no revision on failure, 201 shape, decrease/increase direction, zero reverses, strictly increasing `recorded_at`, replay after newer revisions, reuse, stale, revisions endpoint order and rev 1, third party 404 on a public payment, activity unchanged, statement uses the selected revision once, request-pay payment correctable with its original pay receipt unchanged | `p_corrections` |
| `known_at` before/at `recorded_at` (inclusive) and future, before any record, mid-history; sums in mixed views; statement revision selection; back-dated correction moving a payment between windows and not as known before it; unrecorded payment contributes nothing | `p_known_at` |
| `historical_overdraft` on a back-dated decrease; failure preserves balances, revisions, statements and the key; same-instant combined boundary allowed vs +1 µs refused; `insufficient_funds` precedence; held funds excluded from current affordability | `p_overdraft` |
| Historical available: a back-dated increase that overdraws available while a since-voided hold existed | `p_hold_overdraft` |
| 30 concurrent corrections on one expected revision → one 201 + 29 `stale_revision`; 20 identical retries → one 201 + 19 identical 200s | `p_races` |
| `linked_payment_immutable` for a settlement member and a capture; member revision 1 at `committed_at`; capture once in statements; no hold events in statements | `p_linked` |
| Historical holds: before/at creation, nonfinal capture, void, `known_at` before void runs to the deadline, expiry exactly at `expires_at`, `known_at` before creation/capture, known_at-only view, future as_of past the deadline, clock expiry with `closed_at = expires_at`, final capture closes at capture time, seeded open holds at reset or at `created_at`, seeded closed holds hold nothing | `p_holds` |
| Import chains: stage-1 export (real 2efff63 image) → stage-3 with statements and a correction; stage-2 export (real fc1cdda image) with a partially captured hold and a voided hold → historical held, linked capture, capture after upgrade; stage-3 round trip with views, replay, revisions, repeat import | `p_import_chains`; driver chains 1→3, 2→3, 1→2→3 |
| M3 / M4 in a real browser at 375 px and 1280 px, layout, contrast, no external requests | `ui3.py`; stage-2 `ui_review.py` regression |
| Linearizable random histories including time-travel properties | modeler `driver.py` (new seeds, --steps 300) |

Fault injection (`faults3.py`, 9 plants) results are in `VERDICT.md`.
