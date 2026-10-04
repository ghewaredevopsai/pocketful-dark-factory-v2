# Stage 2 review — gaps in the supplied checks

Revision reviewed: `fc1cdda137ec0fa89033dc85adbd4dce15a99136`. The supplied stage-2 checks are 35 tests
(`kickoff/pocketful/test/stage_2/`, mostly UI). They contain only one holds test (`test_a_hold_reserves_funds_without_moving_them`)
and none for captures, voids, expiry, hold races, the authorizations screen, lost responses, latest-refresh-wins or
layout quality. What they leave out, and what covers it here:

| Gap in supplied checks | Covered by |
|---|---|
| Seeded holds: available derived, past-expiry open seeded as expired, holds > balance → 422 unchanged, holds = balance ok, TTL validation, default TTL 600 | `probes2.py::p_seeded` |
| Authorize: shape, `expires_at = created_at + ttl`, replay/reuse, every error row, held funds refused by payment, request pay, settlement net debit and new holds; settlement net within available commits | `p_authorize` |
| Capture: receiver only (payer/third 403), 404, 0/1.5 → 422, over remaining → `capture_exceeds_authorization`, non-final keeps remainder, final partial releases remainder, cumulative `captured_amount`, `payment_ids`, `remaining_amount`, `{}` vs `{"amount":N}` reuse, replay after close, private capture feed visibility, `authorization_id` on payments | `p_capture` |
| Void: payer only, partial-capture void releases only remainder, void twice no double release, capture after void | `p_void` |
| Clock expiry without any write at the deadline: `/me` releases remainder, list status, `status=open/expired` filters, capture → `authorization_expired`, void → `authorization_not_open`, replay after expiry, no double release, export of expired hold | `p_expiry` |
| Hold imported then expiring by the clock after import | `p_expiry_after_import` |
| Export/import round trip with open partially captured hold, replays of authorize/capture after import, invalid authorization state → 422 | `p_export_import` |
| Upgrade from the accepted stage-1 image (2efff63): token, receipt replay, failed key reusable, pending request payable, TTL default | `p_upgrade`; driver `--stage1-base` seeds 301–315 |
| Races: 40 identical capture retries, 40 distinct-key captures over a remainder, capture/void/payer-spend/new-hold mix at 50 in flight, capture vs clock expiry at the deadline (no capture created at/after `expires_at`, release exactly once) | `p_races`; driver bursts |
| Accept negotiation on `/requests` and `/authorizations` (html vs json vs none) | `p_html` |
| Every data-testid at 375 px and 1280 px, overflow measured per element (body has `overflow-x:hidden`, so scrollWidth alone would hide clipping), visible labels, WCAG AA text contrast, focus ring on 25 tab stops, available as the largest amount | `ui_review.py::u_routes_and_testids`, `u_focus` |
| Loading skeleton, refresh-failure notice, empty states (JPY service) | `u_states` |
| Decimal input rules incl. BHD 3 places, `15.005`/`abc`/`-1`/`1,5` refused without a request, same key+body on resubmit, refused payment keeps every input | `u_pay_form` |
| Lost response after commit → `pay-uncertain`; retry with same key and body moves money once | `u_lost_response` |
| Latest refresh wins with an out-of-order delayed `/me` | `u_latest_refresh_wins` |
| Request cancelled elsewhere → `request-error`, stale pay button removed | `u_requests_flow` |
| BHD split preview equals server shares | `u_split_flow` |
| Holds UI: authorize form, held/available update, capture partial, void, capture of a hold voided elsewhere → `authorization-error` | `u_holds_flow` |
| Pending lost payment across export/import: still signed in, retry recovers, money once | `u_upgrade_pending_retry` |
| No request leaves the service origin; no JS errors | `ui_review.py` network gate on every page |

Fault injection (`faults2.py`): 5 server plants judged by the driver and `probes2.py`, 4 UI plants judged by
`ui_review.py`; results are in `VERDICT.md`.
