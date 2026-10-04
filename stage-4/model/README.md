# Stage 4 reference model and differential driver

Built from the stage-1 to stage-4 requirements text only (UI not modelled); no product source was read.
Python 3 stdlib, no dependencies. Carries the stage-2 model forward: every stage-1/2 rule still holds.

| File | What |
|---|---|
| `RULINGS.md` | every ambiguity and the reading chosen (R1–R92; stage 4 is R81–R92) |
| `model.py` | the executable model: state, operations, results, errors, invariants; historical reads recomputed from revisions and hold events |
| `driver.py` | random sequences against model and product, invariants after every step, time-travel property checks, shrinking |
| `model_server.py` | the model over HTTP — self-check target and fault-injection target (not the product) |

## Run against the product

```sh
# product listening on 8080 (e.g. docker run -e PORT=8080 -p 8080:8080 <image>)
python3 stage-4/model/driver.py --base http://127.0.0.1:8080 --seeds 1-20 --steps 150 --out /tmp/diff-s4
python3 stage-4/model/driver.py --base http://127.0.0.1:8080 --replay /tmp/diff-s4/seed-7.json
# upgrade chains: start on a stage-1, -2 and/or -3 service, export, import into the next stage, continue to stage 4
python3 stage-4/model/driver.py --base S4 --stage1-base S1 --stage2-base S2 --stage3-base S3 --seeds 101-110
```

Flags: `--coverage` prints product status counts per endpoint; `--no-shrink`; `--no-signup`, `--plain-seeded-holds` and `--no-empty-body`
switch off one generator family to look past a known divergence.

Each seed draws a fixture with consistent seeded history (EUR/JPY/BHD, 3–6 users, seeded payments dated in the past
or at reset, seeded requests, operators, seeded holds — some with `created_at`, some expired). It then runs N steps:
- every stage-1/2 operation;
- corrections, valid and invalid: stale, wrong party, linked, unaffordable, historically overdrawn, zero
  amounts, back-dated, replays and key reuse;
- `GET /payments/{id}/revisions`;
- `GET /me` with `as_of`/`known_at`;
- statements with `from`/`to`/`known_at` and paging;
- snapshot paging, including with an extra parameter, another user's token and a token from before a reset;
- bursts: same-expected-revision correction races, snapshot pages during payments and corrections, hold
  races, drains and identical retries;
- export/import, bad resets (including a future seeded `created_at`), and `travel` steps.
- deterministic scenarios: `snapstab` (snapshot stability under corrections, payments, captures and voids, R77a) and,
  in every chain through stage 2, `lifecycle` (holds closed by final capture, void and expiry on stage 2, checked
  inside their lifetimes after the upgrade, R76).

Query instants are drawn from real event instants ± 1 µs/1 ms/1 s/1 h, in several offsets, plus far past and
far future.

Stage 4 adds:
- refunds (valid, over the cap, by the wrong party, of a refund, blocked by holds);
- correction batches: settlement groups with equal instants in different spellings, incomplete groups,
  unequal instants, mixed payments, invalid lists, operator/non-operator;
- batch races in bursts;
- the scenarios `refund_rules`, `settle_batch` and `deadline` (R91–R92), and in plain runs the `roundtrip`
  check of closed holds through export/import (R90).

Server-assigned instants are format- and window-checked, then adopted by the model (RULINGS R53). After that,
historical reads must match the model exactly.

After every step, the driver checks:
- the model's own invariants: sums, openings, available ≥ 0, capture accounting, and recorded times
  strictly increasing;
- `GET /me` for every live user on the product, compared with the model, with balances summing to the seeded
  total.

A `travel` step adds the time-travel property (R79) for 1–3 historical views.

Bursts are checked for linearizability. The first divergence stops a seed; the sequence is shrunk and written as
JSON. Exit code 1 if any seed diverged.

## Self-check

```sh
cd stage-4/model
PORT=18490 python3 model_server.py &                                  # faithful: expect 0 divergences
PORT=18491 MODEL_BUG=no_hist_check python3 model_server.py &          # fault injection, driver must catch it
#   stage 4: refund_no_cap refund_balance correction_below_refund batch_no_completeness batch_no_hist
#            batch_split_recorded late_stamp import_drops_history
#   stage 3: no_hist_check asof_exclusive stmt_page_balance snapshot_live snapshot_live_amount known_at_ignored
#            linked_mutable stale_ignored;  earlier: held_ignored no_expiry capture_closed private_leak overdraft replay_reexecutes
PORT=18381 MODEL_STAGE=1 python3 model_server.py &                    # targets for --stage1-base / --stage2-base
PORT=18382 MODEL_STAGE=2 python3 model_server.py &
PORT=18383 MODEL_STAGE=3 python3 model_server.py &
python3 driver.py --base http://127.0.0.1:18490 --seeds 1-12 --steps 150
python3 driver.py --base http://127.0.0.1:18491 --seeds 1-6 --steps 150 --no-shrink    # expect divergences
python3 driver.py --base http://127.0.0.1:18490 --stage1-base http://127.0.0.1:18381 --stage2-base http://127.0.0.1:18382 --stage3-base http://127.0.0.1:18383 --seeds 101-104
```
