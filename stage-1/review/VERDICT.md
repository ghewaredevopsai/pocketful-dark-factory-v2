# Stage 1 review verdict: ACCEPT 2efff63585ea1163f5a337ad2a88ce07c5ffdd62

Reviewer, 2026-10-04 (times from `date -Is`). Fresh clone at the exact revision in `/tmp/rev-s1`; main repo clean, on `main`.

| Check | Result |
|---|---|
| Harness host (`--out checks/result2-s1-rev-01`) | stage 1 pass, 147/147, suite_digest 9b8fbfb3… |
| Harness isolated (`--out checks/result2-s1-rev-iso`) | stage 1 pass, 147/147 |
| Clean container, `--network none --cpus 2 --memory 2g` | health 200; outbound `Network is unreachable` |
| Differential (modeler driver, new seeds 101–130, 150 steps) | 30 seeds, 0 divergences, 4484 steps, 5985 calls, 393 bursts |
| Reviewer probes (`probes.py`) | 228/228 pass |
| Implementer tests (`test_service.py`) | 45/45 OK |
| Fault injection (`faults.py`, 6 plants) | 6/6 caught by driver, 6/6 by probes |

Minor findings (not blocking):
1. Generated `settlement_id` (`st_<seq>`) is not checked against settlement ids seeded by the fixture, so a new
   settlement can share an id with seeded members (`probe_sid_collision.py`: seeded `st_4`, new settlement → `st_4`,
   members `p_5` and `p_9`). Server line: `sid = st.new_id("st", ())`.
2. Reset hashes seeded passwords serially (~65–80 ms each); 150 users with distinct passwords take 10.86 s, beyond the
   10 s test-control timeout (`probe_reset_scale.py`). Shared passwords are cached (1000 users in 0.09 s); every shipped
   fixture shares one password.
