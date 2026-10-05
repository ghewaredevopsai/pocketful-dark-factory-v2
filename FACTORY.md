# FACTORY.md — a four-seat dark factory with an independent second reading

Team **gheware** · track **pocketful** · Band Desktop 0.4.12 · one human message for the whole run.

## Result

| Stage | Dispatch → coordinator's final report (IST) | Rejections that changed the work | Planted faults caught | Official harness, isolated, fresh clone |
|---|---|---|---|---|
| 1 | 15:40 → 16:31 (51 min) | model ruling changed the product (empty body → 400) | 6 / 6 | claimed stage 1 |
| 2 | 16:31 → 18:15 (1 h 44 min, incl. ~50 min delivery stall) | 2 minor notes carried into stage 3 as ledger items M3/M4 and fixed | 9 / 9 | claimed stage 2 |
| 3 | 18:15 → 23:27 (3 h 12 min, incl. ~40 min delivery stall) | 3 modeler divergences before review (D1–D3), 2 reviewer REJECTs (B1 closed-hold import, B2 capture/expiry race) | 11 / 11 | claimed stage 3 |
| 4 | 23:27 → 00:27 (60 min) | accepted first round | 10 / 10 | claimed stage 4 |

**One dispatch at 15:40:36 IST on 4 Oct; `RUN COMPLETE` at 00:27:01 on 5 Oct: 8 h 46 min wall clock.** The four stage-N
folders each claim exactly their own stage on the event harness in isolated mode, from a fresh clone.

## Seats

| Seat | Harness | Model | Owns | Never does |
|---|---|---|---|---|
| coordinator | Claude Code | claude-sonnet-5-5 | plan, parallel dispatch, ledger audit, routing every finding to its owner, acceptance, opening the next stage | write code, tests or models |
| implementer | Claude Code | claude-opus-5-5 | `LEDGER.md`, product code, its own tests | accept its own work |
| modeler | Claude Code | claude-opus-5-5 | an executable reference model of the spec, `RULINGS.md`, a seeded differential driver | read, import or run product source |
| reviewer | Claude Code | claude-opus-5-5 | fresh-checkout isolated build, supplied checks, ledger audit, differential runs, fault injection, own probes, `GAPS.md`, verdict | fix code or the model |

Every seat commits under its own Git identity (`git log --format=%an | sort | uniq -c`: implementer 30, modeler 10,
reviewer 8; one human commit with mandates and placeholders before the dispatch).

## How it works, and why

1. **One dispatch, self-opening stages.** The human sends one message listing four spec files. The coordinator
   finishes and accepts each stage, posts `FINAL REPORT — stage N`, and opens the next stage itself. No relay, no
   steering: the room holds exactly one human message.
2. **Two independent readings of the spec.** The coordinator hands each stage to the implementer and the modeler *in
   parallel*. The modeler never sees product code; it writes a small, slow, obviously-correct model and a driver that
   fires seeded random operation sequences (valid, invalid, retried, concurrent) at both and reports every divergence
   with a minimal reproduction. A divergence is either a product bug (routed to the implementer) or a reading of the
   spec, settled by a written ruling in `RULINGS.md` (routed to the modeler). *Why:* the shipped checks are 79 / 35 / 9
   / 16 % of each graded suite; a second reading finds what nobody wrote a check for.
3. **Ledger first.** Each stage starts with `LEDGER.md`: one line per normative statement (172 → 321 → 427 → 509
   lines across the stages), audited by the coordinator before code; earlier stages' lines are carried forward.
4. **The reviewer must break things before accepting.** Fresh checkout at the exact revision, image built and run
   with no network, supplied checks, ledger audit, the modeler's driver with new seeds — and **fault injection**:
   at least five plausible defects planted in a scratch copy (a lock released early, an off-by-one overdraft, a
   skipped validation, a late timestamp…). A plant nobody catches is a finding against the checks. Across the run:
   **36 planted, 36 caught.** Then `GAPS.md`: what the supplied checks never asked and which probe covers it.
5. **Bounded repair, every finding traced.** REJECTs go verbatim to the owning seat with a reproduction; fixes come
   back as new commits (never rewritten history) with a regression test; six rounds maximum per stage.
6. **Generic by construction.** Mandates name roles, evidence and gates — no endpoints, fields or error codes. The
   same mandates built the event's practice track (`toy`) before this run: two stages, three real rejects, all fixed.

## A bad result it caught

**Stage 3, B2 — a capture could land after its hold had expired.** Under a capture racing the expiry deadline, the
product checked the clock, then stamped the write a few microseconds later, so a capture could be recorded *after*
`expires_at`. Every supplied check passed. The reviewer's own race probe found it and rejected `47d5454` with a
reproduction at 22:54; at 22:58 the implementer committed `0c6280a`: one instant per locked write, shared by the
expiry check, validation and the recorded time. The reviewer then planted exactly that fault (F11, "stamp after the
clock check") into a scratch copy: its probe caught it on attempt 17 (a late capture of +0.015 ms), while the fixed
build showed no late capture in 60 attempts and no late void in 40.

Before review even started in stage 3, the modeler's differential run found three product defects the shipped
checks never touch: **D1** every historical read for a wallet created by signup returned a 500; **D2** a hold that
was already expired when exported from stage 2 reappeared as held in future-dated views after import; **D3** an
imported hold whose deadline preceded its creation produced a view with held −300. The coordinator relayed each with
a reproduction and a ruling; fixes `4805586` and `d21365f` came back with regression tests.

## Cost and time

Wall clock 8 h 46 min including two delivery stalls (~90 min, see Incidents). Per seat, summed from the seats' own
Claude Code transcripts (deduplicated per API message) and priced at Anthropic list rates ($/MTok — Opus 5.5: 4 in,
20 out, 5 cache-write, 0.20 cache-read; Sonnet 5.5: 2, 10, 2.50, 0.20):

| Seat | Model | API calls | Output tokens | Cache-write tokens | Cache-read tokens | List-price USD |
|---|---|---|---|---|---|---|
| coordinator | claude-sonnet-5-5 | 64 | 46,607 | 306,863 | 7,558,948 | $2.75 |
| implementer | claude-opus-5-5 | 302 | 332,786 | 895,778 | 60,796,562 | $23.30 |
| modeler | claude-opus-5-5 | 209 | 377,689 | 959,120 | 49,812,269 | $22.31 |
| reviewer | claude-opus-5-5 | 193 | 274,148 | 1,485,620 | 68,046,787 | $26.52 |
| **total** | | | | | | **$74.88** |

The seats ran on a Claude subscription, so the marginal cash spend was the subscription; the table is the list-price
equivalent. Tool: `tools/seat_cost.py`.

## Incidents (disclosed)

Twice a seat's reply was **staged but never posted** by Band: its turn ended with a background-task error, the reply
was lost, and the coordinator waited for it (stage 2 after the modeler's report, ~50 min; stage 3 after the
implementer's fix report, ~40 min). The operator ran `band restart` on that one seat's runtime for this room; Band
re-delivered the unsettled message and the seat posted its report. **No message was posted into the room** — the only
human message remains the dispatch. Both events are timestamped in the room log.

## Setup (another team)

1. Install Band Desktop, `band init` (browser sign-in), `band plugin install`, `band preflight`.
2. Create an empty result repository and the four seats:
   ```sh
   band agent create --session seat-<name> --name <name> --description "Dark-factory seat: <name>" \
     --transport claude-code-cli --runtime-auth subscription --runtime-model <model> \
     --claude-context-mode local_config --claude-strict-mcp-config \
     --cwd <absolute result repo> --instructions-file mandates/<name>.md
   ```
3. In the Band console, create a room as the human with all four seats.
4. Send one message to `@coordinator` (ours is `dispatch/run2-pocketful.md`): task, absolute repo path, one spec
   path per stage (stage 1 inline), the check command, and the exact first line of each stage report. Send nothing
   else.
5. Watch for `RUN COMPLETE`. If a seat's queue holds a message long after its last activity, restart that seat's
   runtime (`band restart --session seat-<name> --host-session default-<room id>`); never post into the room.

## What we tried that failed

- **Run 1 (published earlier, kept for reference):** three seats, four human dispatches, one Git identity, a reviewer
  mandate that said "floating-point money" (a track hint). It reached stage 4 in ~4 h for $78.90, but a competitor
  review showed the stronger entries had an independent model, per-seat identities and one dispatch. Run 2 adds all
  three and removes the track hint.
- **A relay script** that sent stage dispatches when it saw a report missed run 1's stage-1 report (wrong wording)
  and once ran the harness from the wrong directory. Self-opening stages replaced it.
- **Seat-owned rooms** (`band chat new` from a seat) cannot be dispatched into by the human; create rooms in the
  console. **Bare-context Claude Code seats** refuse subscription auth.
- **Isolated checks on our host** needed a local patch: the judge-runner image's browser download picked an
  unreachable IPv6 address. Suites untouched.

## Limits — what we do not claim

- The shipped checks are a fraction of the graded suite; our differential models and probes are our own reading of
  the spec, and a ruling in `RULINGS.md` can be a wrong reading the band agreed on.
- Fault injection shows the checks catch the faults we thought of, not all faults.
- Costs are list-price equivalents computed from transcripts, not an invoice.
- Wall-clock times include the two Band delivery stalls.
