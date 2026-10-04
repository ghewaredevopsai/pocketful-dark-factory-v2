# Stage 4 review — gaps in the supplied checks

Revision reviewed: `0d5e3cc1064adcc659f5494068c57bbd335fe36c`. The supplied stage-4 checks are 5 tests. What they do
not cover, and what covers it here:

| Gap | Covered by |
|---|---|
| Refund shape (direction, `refund_of`, null links, original note/visibility), replay 200, key reuse 409, 401/400/403 sender/403 third party/404, 8 invalid amounts, cumulative cap, exact remainder, refund-of-refund (422 for its receiver, 403 for its sender), correcting a refund is immutable | `probes4.py::p_refunds` |
| Refund cap follows the current corrected amount; correction floor at the refunded total (149 refused, 150 allowed); fully refunded payment | `p_refunds` |
| Refund funded from available, not total (held funds refused); failure changes nothing; key unclaimed after 409 | `p_refunds` |
| Refunds never reopen a request (still paid, pay again 409) or an authorization (held unchanged, remainder still capturable, final-captured stays captured); private refund hidden from third parties; settlement member refundable, refund has `settlement_id` null, membership and feed unchanged; `refund_of: null` elsewhere | `p_refund_links` |
| Refunds in statements (both sides), `as_of` boundary at `created_at`, `known_at` before the refund, earlier snapshot frozen, sums in mixed views | `p_refund_history` |
| Batch 401/403/400, empty body 400, 6 malformed shapes, 404, 5 item validation rows, stale, linked captures and refunds, refund floor in a batch | `p_batch_matrix` |
| Precedence: item errors in input order (stale vs 404 both ways, 422 before later 404), item errors before completeness, completeness before funds, differing member instants 422, one instant in 3 offset spellings accepted | `p_batch_matrix` |
| Response shape, shared `recorded_at` strictly after every member's previous revision, `correction_batch_id` on revisions (null on rev 1), replay 200, reuse 409, settlement replay returns the original receipt, feed keeps original amounts, statements use batch revisions, single member correction still immutable, `known_at` before the batch | `p_batch_matrix` |
| Combined affordability both ways (items singly affordable but not together → 409 with nothing changed and the key unclaimed; an increase unaffordable alone, funded by another item → 201) | `p_batch_funds` |
| `historical_overdraft` in a batch with atomic rejection; same-instant combined boundary accepted; snapshot before the batch frozen; sums in views | `p_batch_history` |
| 30 concurrent batches/singles sharing one expected revision → exactly one 201; refunds racing a correction keep refunded ≤ current amount | `p_batch_races` |
| One clock instant per write: refunds and batch debits funded by a hold expiring mid-burst are accepted only at/after `expires_at` and never historically unaffordable | `p_deadline`; stage-3 `probe_capture_after_deadline.py`, `probe_void_after_deadline.py`, `probe_late_stamp_other.py` |
| Stage-3 export (real 0c6280a image) with a settlement, a correction and a snapshot → stage 4: snapshot pages, revisions, replay, membership (single immutable, incomplete batch 422), complete batch, refund to corrected amount; stage-4 round trip with batch ids, refund links, snapshots, caps and strictly later new batch | `p_imports`; driver chains 1→4, 2→4, 3→4, 1→2→3→4 |
| 50 in flight with refunds, identical refund retries and statements: no 5xx | `p_concurrency` |
