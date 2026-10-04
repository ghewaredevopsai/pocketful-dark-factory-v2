# Stage 2 review verdict: ACCEPT fc1cdda137ec0fa89033dc85adbd4dce15a99136

Reviewer, 2026-10-04 (times from `date -Is`). Fresh clone at the exact revision (`/tmp/rev-s2`). Main repo clean, on
`main` at bfeec97, which differs from fc1cdda only by `stage-2/model/`. `git diff 2efff63 HEAD -- stage-1` touches only
`stage-1/review/`.

| Check | Result |
|---|---|
| Harness host (`checks/result2-s2-rev-01`) | stage 1 147/147, stage 2 35/35, claimed stage 2 (suite 3c6cf7f8…) |
| Harness isolated (`checks/result2-s2-rev-iso`) | stage 1 147/147, stage 2 35/35, claimed stage 2 |
| Clean container on `--internal` network, `--cpus 2 --memory 2g` | health 200; outbound `Network is unreachable` |
| Differential, seeds 201–230, `--steps 300` | 30 seeds, 0 divergences, 8993 steps, 11467 calls, 603 bursts, 66 captures of expired holds |
| Upgrade differential from the accepted stage-1 image 2efff63, seeds 301–315, `--steps 300` | 15 seeds, 0 divergences, 4495 steps, 5964 calls, 358 bursts |
| Stage-1 probes against the stage-2 image | 227/228 (one expected difference: `/me` now carries total/available/held, as stage 2 requires) |
| Stage-1 minor findings | M1 fixed (seeded `st_4` → new `st_5`); M2 fixed (150 distinct passwords reset in 3.01 s) |
| `probes2.py` (holds, captures, voids, clock expiry, races, upgrade, Accept) | 141/141 |
| `ui_review.py` (Chromium, 375 px and 1280 px, external requests aborted) | 198/198, 0 external requests, 0 JS errors |
| Implementer tests | `test_service.py` 63 OK; `test_ui.py` 16 OK (1 skipped) |
| Fault injection (`faults2.py`) | 9 planted, 9 caught. U1 was missed at first because of a probe defect (blocking sync route delay); after the probe was fixed to an in-page delay, the U1 copy fails and the real build passes |

## UI judgment

Calm, coherent finance look: a green wallet card with **Available to spend** as the headline (largest type on the
page), total balance and amount on hold secondary, and the amount on hold in amber. Cards and controls use a
consistent type scale and spacing, with one filled primary button per form. Status badges are distinct (pending
blue, paid/collected green, held amber, private purple, released/expired neutral). The uncertain notice is amber
with a "?" mark, refusals are red, success is green. Labels are always visible, a 3 px focus ring appears on every
tab stop, and all text passes WCAG AA. Nothing overflows at 375 px, even with 200-character notes and 20-character
handles. Empty, loading-skeleton and refresh-error states are all designed. Screenshots are in `screenshots/`.

Minor findings (not blocking):
1. `/authorizations` labels every closed hold "Expired <expires_at>", including collected and released ones whose
   expiry is in the future (`screenshots/authorizations-1280.png`: "Released … Expired Oct 4, 2026, 7:58 PM",
   taken at 17:58). Expected: no expiry wording for captured/voided holds, or "Would have expired".
2. At 375 px the activity feed comes after all three forms (pay, request, hold), so it is about 1,400 px down
   (`screenshots/wallet-375.png`). This is usable but not ideal for scanning.
