# Stage 1 reference model and differential driver

Built from the stage-1 requirements text only; no product source was read. Python 3 stdlib, no dependencies.

| File | What |
|---|---|
| `RULINGS.md` | every ambiguity and the reading chosen |
| `model.py` | the executable model: state, operations, results, errors, §1 invariants |
| `driver.py` | random sequences against model and product, invariants after every step, shrinking |
| `model_server.py` | the model over HTTP — self-check target and fault-injection target (not the product) |

## Run against the product

```sh
# product listening on 8080 (e.g. docker run -e PORT=8080 -p 8080:8080 <image>)
python3 stage-1/model/driver.py --base http://127.0.0.1:8080 --seeds 1-20 --steps 150 --out /tmp/diff-s1
python3 stage-1/model/driver.py --base http://127.0.0.1:8080 --replay /tmp/diff-s1/seed-7.json
```

Each seed draws a fixture (EUR/JPY/BHD, 3–6 users, seeded payments/requests, 0–2 operators, sometimes a
balance near 2^53), resets both sides and runs N steps: payments, requests, pay/decline/cancel, splits,
settlements, feed and request listings with valid and invalid query parameters, signup/login, bad tokens,
retries and replays (same key + same body via another session, reformatted body, changed body, same key on
another path), concurrent bursts of 2–8 requests (identical retries, wallet drains, mixes),
export/import (good and invalid), and resets with an invalid fixture. Seeds make runs repeatable
(bursts are concurrent, so their interleaving is not).

After every step: the model's own invariants, then `GET /me` for every live user on the product compared with
the model, balances non-negative, product balances summing to the seeded total.

Bursts are checked for linearizability: some sequential order of the burst must explain every product
response; the model continues from that order.

The first divergence stops a seed; the sequence is shrunk (product reset before each attempt) and written as
JSON. Exit code 1 if any seed diverged.

## Self-check

```sh
cd stage-1/model
PORT=18090 python3 model_server.py &                          # faithful: expect 0 divergences
PORT=18091 MODEL_BUG=private_leak python3 model_server.py &   # also: overdraft, replay_reexecutes
python3 driver.py --base http://127.0.0.1:18090 --seeds 1-12 --steps 150
```
