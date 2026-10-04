# Stage 3 review verdict, repair round 2: ACCEPT 0c6280a338f493cd6cce90bc5edb339e457934ab

Reviewer, 2026-10-04 (times from `date -Is`). Fresh clone at the exact revision (`/tmp/rev-s3c`). Main is clean and on `main`;
HEAD = 0c6280a. Since their accepted revisions, `stage-1/` changed only in `review/` and `stage-2/` only in `review/`
and `model/`. Earlier rounds: REJECT d21365f (B1, 15e5a72/c9c6145) and REJECT 47d5454 (B2, 40724e7/eacc1b0).

## B2 (round 1): fixed

`State.begin_op()` takes one instant per locked write (`ts = st.stamp()`, `st.expire(ts)`). Validation and every
recorded time in that write use it, including correction `effective_at <= now` (`st.now()`).
- `probe_capture_after_deadline.py`: "no late capture in 20 attempts" and "no late capture in 60 attempts". The same
  probe catches the planted F11 (stamp after the clock check) at attempt 17: "+0.015 ms … status expired".
- `probe_void_after_deadline.py`: "0 of 40 voids closed at/after expires_at".
- `probe_late_stamp_other.py` (payments and new holds funded by a hold expiring mid-burst, and corrections with
  `effective_at` = client now, over 5 rounds): "TOTAL 350 FAIL 0". Exactly 2 of 60 writes of 50 are accepted before
  expiry (100 available) and 18 after the release. No accepted write is unaffordable at its own recorded instant,
  and no correction records `effective_at` after `recorded_at`.

## Dispute (probes2 p_races section 4): probe timing assumption, not a product defect

The section fires 120 nonfinal captures of 10 at a 1000 hold. Those total 1200, so when 100 complete before the
deadline the hold is exhausted. The other 20 are then correctly `409 authorization_not_open` and the hold ends `captured`.
`probe_exhaust_before_deadline.py` forces that case: "responses: {'201 ': 100, '409 authorization_not_open': 20} …
status captured captured_amount 1000 … captures at/after expires_at: 0 … correct behaviour". I fixed
`stage-2/review/probes2.py` to accept `authorization_not_open` only when the hold is fully captured. The
late-capture check is unchanged. On 0c6280a: 141/141.

## Evidence

| Check | Result |
|---|---|
| Harness host (`checks/result2-s3-rev3-01`) | s1 147/147, s2 35/35, s3 6/6, claimed stage 3 (suite c41848dd…) |
| Harness isolated (`checks/result2-s3-rev3-iso`) | same counts, claimed stage 3 |
| Container on `--internal` network, `--cpus 2 --memory 2g` | health 200; outbound `Network is unreachable` |
| Differential (model 67505a7), plain 901–930 `--steps 300 --coverage` | 30 seeds, 0 divergences, 10263 calls, 370 bursts, 101 time-travel |
| Chain 1→3, 541–570 | 30 seeds, 0 divergences |
| Chain 2→3, 641–680 | 40 seeds, 0 divergences |
| Chain 1→2→3, 741–780 | 40 seeds, 0 divergences |
| `probes3.py` | 175/175 |
| `probe_s2_closed_hold_history.py` / `probe_s2_seeded_closed_holds.py` | 6/6 / 4/4 |
| Stage-1 probes / stage-2 `probes2.py` (fixed) / stage-2 `ui_review.py` | 227/228 (expected `/me` shape) / 141/141 / 198/198 |
| `ui3.py` (M3, M4; external requests aborted) | 26/26 |
| Implementer tests at 0c6280a | `test_service.py` 93 OK; `test_ui.py` 18 OK (1 skipped) |
| Fault injection (`faults3.py`, F1–F11; F11 = capture/void stamped after the clock check) | 11 planted, 11 caught (driver 9; F10 and F11 are caught only by the stage-2 history and late-capture probes) |

Waived: nothing. A note for the modeler: the driver does not catch F10 (no upgrade in its plain runs) or F11 (no
capture fired across the deadline).
