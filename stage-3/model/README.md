# Stage 3 reference model and differential driver

Built from the stage-1, stage-2 and stage-3 requirements text only (UI not modelled); no product source was read.
Python 3 stdlib, no dependencies. Carries the stage-2 model forward: every stage-1/2 rule still holds.

| File | What |
|---|---|
| `RULINGS.md` | every ambiguity and the reading chosen (R1–R80; stage 3 is R53–R80) |
| `model.py` | the executable model: state, operations, results, errors, invariants; historical reads recomputed from revisions and hold events |
| `driver.py` | random sequences against model and product, invariants after every step, time-travel property checks, shrinking |
| `model_server.py` | the model over HTTP — self-check target and fault-injection target (not the product) |

## Run against the product

```sh
# product listening on 8080 (e.g. docker run -e PORT=8080 -p 8080:8080 <image>)
python3 stage-3/model/driver.py --base http://127.0.0.1:8080 --seeds 1-20 --steps 150 --out /tmp/diff-s3
python3 stage-3/model/driver.py --base http://127.0.0.1:8080 --replay /tmp/diff-s3/seed-7.json
# upgrade chains: start on a stage-1 and/or stage-2 service, export, import into the stage-3 service, continue
python3 stage-3/model/driver.py --base S3 --stage1-base S1 --stage2-base S2 --seeds 101-110
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

Query instants are drawn from real event instants ± 1 µs/1 ms/1 s/1 h, in several offsets, plus far past and
far future.

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
cd stage-3/model
PORT=18390 python3 model_server.py &                                  # faithful: expect 0 divergences
PORT=18391 MODEL_BUG=no_hist_check python3 model_server.py &          # fault injection, driver must catch it
#   stage 3: no_hist_check asof_exclusive stmt_page_balance snapshot_live known_at_ignored linked_mutable
#            stale_ignored;  earlier: held_ignored no_expiry capture_closed private_leak overdraft replay_reexecutes
PORT=18381 MODEL_STAGE=1 python3 model_server.py &                    # targets for --stage1-base / --stage2-base
PORT=18382 MODEL_STAGE=2 python3 model_server.py &
python3 driver.py --base http://127.0.0.1:18390 --seeds 1-12 --steps 150
python3 driver.py --base http://127.0.0.1:18391 --seeds 1-6 --steps 150 --no-shrink    # expect divergences
python3 driver.py --base http://127.0.0.1:18390 --stage1-base http://127.0.0.1:18381 --stage2-base http://127.0.0.1:18382 --seeds 101-104
```
