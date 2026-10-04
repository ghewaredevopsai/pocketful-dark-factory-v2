# Stage 3 review verdict, repair round 1: REJECT 47d5454a5647517bfd24eb59be8472d4934759bc

Reviewer, 2026-10-04 (times from `date -Is`). Fresh clone at the exact revision (`/tmp/rev-s3b`). Main is clean, on `main`
at 67505a7, which differs from 47d5454 only in `stage-3/model/`. Since their accepted revisions, `stage-1/` changed only
in `review/` and `stage-2/` only in `review/` and `model/`. The round-0 verdict (REJECT d21365f, B1) is in git history (15e5a72, c9c6145).

## B1 (round 0): fixed

`probe_s2_closed_hold_history.py`: TOTAL 6 FAIL 0 (final-captured, voided-after-nonfinal-capture and expired stage-2
holds now hold inside their lifetimes after import). D2/D3 kept: `probe_s2_seeded_closed_holds.py` TOTAL 4 FAIL 0
(holds seeded captured/voided/expired, including expired with a future deadline, hold nothing; a seeded open hold
holds from reset to its deadline).

Coordinator ruling on void instants: accepted. A stage-2 export records no void time, so no stage-3 service can do
better than a window. The product closes at the window's lower bound, the latest known event, which lies inside
[latest known event, end of import].

## Blocking finding

**B2 — a capture can be recorded after its hold's `expires_at` (capture vs clock-expiry race).**
Requirements:
- "`expires_at` is at or before now → 409 `authorization_expired`"
- "A closed hold cannot be captured again"
- "Expiry takes effect at `expires_at`"
- "Concurrent requests must produce the same results as executing them one at a time in some order, and the
  requirements above hold at every read."

Cause, from reading the code: `route()` runs `st.expire()` at lock entry using one `now_dt()`. `h_capture` validates
against that state, then stamps the capture with a later `st.stamp()`. When the deadline falls between those two
instants, the capture passes the expiry check but is recorded at or after `expires_at`. The hold is later closed as
`expired` at `expires_at`. History then shows the hold released at `expires_at` and a capture from it afterwards. No
one-at-a-time order at the recorded instants explains that.

Reproduction: `python3 stage-3/review/probe_capture_after_deadline.py <base> 20`. It uses TTL 1 s and fires 120
nonfinal captures from 30 threads across the deadline, retrying with fresh holds.

```
attempt 5: 1 capture(s) recorded at/after expires_at
  expires_at          2026-10-04T17:23:21.475232+00:00
  late capture        2026-10-04T17:23:21.475282+00:00  (+0.050 ms) payment p_143 amount 5
  authorization       status expired captured_amount 355 closed_at 2026-10-04T17:23:21.475232+00:00
  ada as_of expires_at     total 99650 held 0 available 99650
  ada as_of late capture   total 99645 held 0 available 99645
  bob statement: capture entries after expires_at: 1
```

It also reproduced in the stage-2 regression probe (`probes2.py` "no capture created at/after expires_at", 2 of 6 runs
under load). The accepted stage-2 image fc1cdda has the same defect (+0.001 ms at attempt 3, +0.012 ms at attempt 16).
It predates stage 3, and I missed it in the stage-2 review because the race did not occur there under light load.

Expected fix: use one instant per locked operation. Take `ts = st.stamp()` first, call `st.expire(ts)`, then validate
and record with that same `ts`; or re-check `a["_exp"] <= ts` after stamping and answer 409 `authorization_expired`.
`h_void` closes with a later `st.stamp()` after the same lock-entry expiry, so it has the same pattern. I did not see it
fail in 40 single-request attempts (`probe_void_after_deadline.py`).

## Evidence that passed

| Check | Result |
|---|---|
| Harness host (`checks/result2-s3-rev2-01`) | s1 147/147, s2 35/35, s3 6/6, claimed stage 3 (suite c41848dd…) |
| Harness isolated (`checks/result2-s3-rev2-iso`) | same counts, claimed stage 3 |
| Container on `--internal` network, `--cpus 2 --memory 2g` | health 200; outbound `Network is unreachable` |
| Differential (model 67505a7), plain 801–830, `--steps 300 --coverage` | 30 seeds, 0 divergences, 10274 calls, 375 bursts, 101 time-travel |
| Chain 1→3, 511–540 | 30 seeds, 0 divergences |
| Chain 2→3, 601–640 (incl. 606) | 40 seeds, 0 divergences |
| Chain 1→2→3, 701–740 (incl. 706) | 40 seeds, 0 divergences |
| `probes3.py` | 175/175 |
| `probe_s2_closed_hold_history.py` / `probe_s2_seeded_closed_holds.py` | 6/6 / 4/4 |
| Stage-1 probes / stage-2 `probes2.py` / stage-2 `ui_review.py` vs 47d5454 | 227/228 (expected `/me` shape) / 140/141 (B2) / 198/198 |
| `ui3.py` (M3, M4) | 26/26, no external requests |
| Implementer tests at 47d5454 | `test_service.py` 91 OK; `test_ui.py` 18 OK (1 skipped) |
| Fault injection (`faults3.py`, F1–F10, F10 = closed stage-2 holds lose history) | 10 planted, 10 caught (driver 9, probes 10; F10 is caught by the stage-2 history probe, while the driver's plain runs have no upgrade) |
