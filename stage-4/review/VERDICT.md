# Stage 4 review verdict: ACCEPT 0d5e3cc1064adcc659f5494068c57bbd335fe36c

Reviewer, 2026-10-05 (times from `date -Is`). Fresh clone at the exact revision (`/tmp/rev-s4`). Main is clean, on `main`
at 9602956, which differs from 0d5e3cc only in `stage-4/model/`. Since their accepted revisions, `stage-1/`, `stage-2/`
and `stage-3/` changed only in `review/` and `model/`.

| Check | Result |
|---|---|
| Harness host (`checks/result2-s4-rev-01`) | s1 147/147, s2 35/35, s3 6/6, s4 5/5, claimed stage 4 (suite 7808dfc4…) |
| Harness isolated (`checks/result2-s4-rev-iso`) | same counts, claimed stage 4 |
| Container on `--internal` network, `--cpus 2 --memory 2g` | health 200; outbound `Network is unreachable` |
| Differential (model 9602956, `--steps 300 --coverage`, no skip options), plain 1001–1030 | 29 ok; seed 1029 is a model-side divergence (below) |
| Chain 1→4, 1101–1120 | 20 seeds, 0 divergences |
| Chain 2→4, 1201–1220 | 19 ok; seed 1210 diverged while still on the frozen stage-2 service (below) |
| Chain 3→4, 1301–1330 | 30 seeds, 0 divergences |
| Chain 1→2→3→4, 1401–1430 | 30 seeds, 0 divergences |
| `probes4.py` (stage 4) | 128/128 |
| Stage-1 probes / stage-2 `probes2.py` / stage-3 `probes3.py` vs 0d5e3cc | 227/228 (expected `/me` shape) / 141/141 / 175/175 |
| Stage-3 deadline and import probes vs 0d5e3cc | closed-hold history 6/6, seeded closed holds 4/4, no late capture in 20, 0 of 40 late voids, late-stamp 146/146, exhausted-hold case correct |
| Stage-2 `ui_review.py` / stage-3 `ui3.py` | 198/198 / 26/26, no external requests (screenshots in `screenshots/`) |
| Implementer tests at 0d5e3cc | `test_service.py` 111 OK; `test_ui.py` 18 OK (1 skipped) |
| Fault injection (`faults4.py`, G1–G10) | 10 planted, 10 caught (driver 9; G7 late stamp is caught only by the late-capture probe; G10 only by the stage-3-import snapshot check in probes4) |

Divergences investigated (neither is a stage-4 product defect):
- Plain seed 1029 reproduces deterministically on replay. Hold `a_49` received two identical concurrent captures of
  1: `p_51` at `…22.011087` and `p_53` at `…22.030736`, both before `expires_at …22.049195`. The product reports
  `payment_id: p_53` (the latest capture) and `payment_ids: [p_51, p_53]` in capture order. The model mapped its two
  symmetric burst captures to the receipts in the other order. Modeler: map same-body burst receipts by `created_at`.
- Chain 2→4 seed 1210 diverged at step 70, while the run was still on the stage-2 service fc1cdda (the upgrade comes at
  step 150). It was a capture burst on hold `ma47` (TTL 3 s) straddling its deadline. That is the stage-2 capture
  stamp race fixed in stage 3 (B2); stage 2 is frozen. Not reproduced in 3 replays.

Minor (hygiene, not product): HEAD 9602956 commits `stage-4/model/__pycache__/*.pyc`.
Waived: nothing.
