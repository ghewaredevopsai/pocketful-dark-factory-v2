# Stage 3 review verdict: REJECT d21365f0e437e4165ba4b4b5af45256cdfadd00e

Reviewer, 2026-10-04 (times from `date -Is`). Fresh clone at the exact revision (`/tmp/rev-s3`). Main is clean, on `main`
at 565e9e5, which differs from d21365f only in `stage-3/test_service.py`. Since their accepted revisions, `stage-1/`
changed only in `review/` and `stage-2/` only in `review/` and `model/`.

## Blocking finding

**B1 — holds created through the stage-2 API that were closed before the upgrade lose their history after import
into stage 3.**
Requirements: "A hold starts at authorization creation; nonfinal capture reduces it at capture time; final capture,
void or expiry releases the remainder at that event's time. Expiry takes effect at `expires_at`" and "A stage-3
service must accept exports produced by the same team's stage-1 or stage-2 service. The ledger must import and
account for authorizations and captures." Only *seeded* closed holds are exempt ("seeded closed holds need not
reconstruct a prior lifecycle"). The ledger's own decision says: "Stage-2 exports' holds get their lifecycle from creation
time and capture payments; a voided hold from a stage-2 export closes at its latest known event." The code
(`lifecycle_in`, else branch) reconstructs only holds still open at export. Every captured, voided or expired hold gets
`created: null` and contributes nothing to any historical view.

Reproduction (`probe_s2_closed_hold_history.py <stage-2 fc1cdda base> <stage-3 d21365f base>`). On the stage-2 service:
- authorize 1000, then final capture 300;
- authorize 400, nonfinal capture 100, then void;
- authorize 500 with TTL 2 s and let it expire.

Then export, import into stage 3, and read `/me?as_of=` inside each hold's lifetime:

```
FAIL final-captured hold: between creation and capture       want (total, held, available) (10000, 1000, 9000) got (10000, 0, 10000)
FAIL voided hold: between creation and its nonfinal capture  want (total, held, available) (9700, 400, 9300) got (9700, 0, 9700)
FAIL expired hold: before its deadline                       want (total, held, available) (9600, 500, 9100) got (9600, 0, 9600)
```

The modeler's driver found the same defect independently in the 1→2→3 chain, seed 706, step 218:
`$.available: expected integer 5265, got 7199`. In the reproduced run, stage-2 hold `a_37` (1934, created 14:47:05.375,
final-captured 14:47:05.556) is exported with `lifecycle.created = null`, and `/me?as_of=14:47:05.536` shows held 0.
The divergence is timing-dependent on replay because capture timing varies. Seed 606 in the 2→3 chain diverged the
same way (`available` too high); it was not reproduced in 13 replays and is probably the same class.

Expected fix: reconstruct imported stage-2 holds from `created_at`, capture payments (`authorization_id` = the hold)
and the close event (final capture time, `expires_at`, or for voids the latest known event, as the ledger says). The
existing `close <= ct` rule already makes seeded closed holds (created at reset, no later events) contribute nothing.

## Evidence that passed

| Check | Result |
|---|---|
| Harness host (`checks/result2-s3-rev-01`) | s1 147/147, s2 35/35, s3 6/6, claimed stage 3 (suite c41848dd…) |
| Harness isolated (`checks/result2-s3-rev-iso`) | same counts, claimed stage 3 |
| Container on `--internal` network, `--cpus 2 --memory 2g` | health 200; outbound `Network is unreachable` |
| Differential, seeds 401–430, `--steps 300`, `--coverage` | 30 seeds, 0 divergences, 10889 calls, 456 bursts, 133 time-travel checks |
| Chain 1→3, seeds 501–510 | 10 seeds, 0 divergences |
| Chain 2→3, seeds 601–610 | 9 ok, seed 606 diverged (see B1) |
| Chain 1→2→3, seeds 701–710 | 9 ok, seed 706 diverged (B1, reproduced with export) |
| Stage-1 probes vs stage-3 image | 227/228 (expected: `/me` gained total/available/held in stage 2) |
| Stage-2 `probes2.py` / `ui_review.py` vs stage-3 image | 141/141 / 198/198 |
| `probes3.py` (stage-3 API) | 175/175 |
| `ui3.py` (M3, M4; 375 px and 1280 px; external requests aborted) | 26/26; M3 and M4 fixed |
| Implementer tests at 565e9e5 | `test_service.py` 84 OK; `test_ui.py` 18 OK (1 skipped) |
| Fault injection (`faults3.py`) | 9 planted, 9 caught by probes3 (driver caught 8: it misses F3 snapshot entries showing the live corrected amount) |

Not a product finding: in the 606 replays the seeded-hold instant is self-consistent (`probe_seeded_hold_instant.py`:
created_at inside the reset window; held at C, not at C−1 µs).
