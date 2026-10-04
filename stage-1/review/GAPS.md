# Stage 1 review — gaps in the supplied checks

Revision reviewed: `2efff63585ea1163f5a337ad2a88ce07c5ffdd62`. Supplied checks: 147 tests in
`kickoff/pocketful/test/stage_1/` (harness host and isolated). What they never ask, and what covers it here:

| Gap in supplied checks | Covered by |
|---|---|
| Concurrent identical retries at 50 in flight (supplied: one wallet under ten clients) | `probes.py::p_concurrency` (50 retries → one 201 / 49 × 200 identical), driver bursts (393 bursts, 30 seeds) |
| Wallet drain race: N concurrent distinct payments, exactly floor(balance/amount) succeed | `p_concurrency` drain (10 × 201 / 40 × 409) |
| Same request paid concurrently under different keys moves money once | `p_concurrency` (1 × 201 / 29 × 409 `request_not_pending`) |
| Global consistency after a mixed 400-op burst (payments + settlements + feed reads) | `p_concurrency` mixed burst: sum conserved, no negative, no 5xx |
| Concurrent same-email signup creates one account | `p_concurrency` (1 × 201 / 19 × 409) |
| Login latency at 50 concurrent (scrypt) | `p_concurrency` (< 5 s) |
| Reset time with many distinct seeded passwords (10 s control timeout) | `p_reset_many_passwords` (100 users) |
| Balance ceiling 2^53: credit to exactly 2^53 ok, beyond rejected without 5xx | `p_overflow` |
| Settlements: 401/403/400 key, replay identical, net affordability (chain funded inside the batch), all-or-nothing, entry-order precedence (404 vs 422 both orders, entry error before funds), 0/32/33 entries, non-object entry, failed key reusable, operator gets no private visibility nor request access | `p_settlements` |
| Settlement membership with fixture-seeded `settlement_id` (id uniqueness) | `probe_sid_collision.py` (minor finding) |
| Seeded `created_at` with a non-UTC offset survives round trip; seeded paid request is not payable; seeded balances not replayed | `p_seeded_readable` |
| Export/import: snapshot isolation, old tokens valid, receipt and settlement replays 200 identical after import, failed key reusable after import, operator kept, repeat import no duplication, new ids unique after import, all invalid-import rows 422/400 with no change, post-export credentials removed, reset clears imported tokens | `p_export_import`, driver (export/import steps) |
| Pay replay after funds arrive on a key that first failed 409; `{}` vs `{"visibility":"public"}` reuse; empty body = public | `p_requests` |
| Decline/cancel full state matrix (twice, cross-state 409s, 403s, 404) | `p_requests` |
| `GET /requests` exact `has_more` at boundary, offset past end, every invalid query row | `p_requests`, `p_feed` |
| Feed paging through 30 same-second payments without dup/gap | `p_feed` |
| Split: zero share produces a payable 0-amount request; caller omitted; replay creates no new requests; conservation after paying all splits | `p_splits` |
| Amount forms `1e3`, `1000.0`, `1E1`; NaN, `1e400`, non-UTF-8, 100k-deep nesting never 5xx | `p_payments`, `p_http_edges` |
| Idempotency-Key 255 ok / 256 → 422; key with invalid body after success → reuse | `p_payments` |
| Plaintext password never in export | `p_auth` |
| Linearizability of concurrent mixes against a reference model | modeler `driver.py`, seeds 101–130 (new seeds) |

Fault injection (`faults.py`) tests whether these probes and the driver have teeth; results are in the verdict.
