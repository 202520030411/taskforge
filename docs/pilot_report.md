# P3 pilot report: one bad pattern, ten proposals, real models

Run `run-f7c7f10fca`, started 2026-11-16 22:00 ET, exported 2026-11-17 09:32 ET. Claude Opus 5.5 for every model role; Qwen3-Coder-30B-A3B (via OpenRouter, pinned to Amazon Bedrock) as the weak model. Caps: Anthropic $30, OpenRouter $15. Decisions behind the numbers: `docs/decisions.md` D-028 to D-033.

## Result

| | |
|---|---|
| Proposals | 10, all for one pattern: **skips-end-to-end-verification** |
| Accepted | **6** (4 directly, 2 after one difficulty repair) |
| Rejected | 4: 3 dropped by a person at H3 (repairs did not land the task in range), 1 whose strengthened tests broke the reference solution |
| Total spend | **$27.09**: Anthropic $23.07, OpenRouter (Qwen) $4.02 |
| Per accepted task | $4.52 including pattern mining; $2.97 without it (mining is reusable, below) |
| Requests paid twice | **0**, across 4,269 paid model calls, 15 steps taken over after worker restarts, and 404 replies served again from the cache |
| Machine time | 22:00 to 23:20 ET (80 min, including a 20-minute pause during the host outage); the rest of the wall time was the run waiting for reviews overnight |

## The funnel

```
10 proposals (overlap with TB 2.1: 0.0 for all; 3 sampled at H2, all kept)
 └─ build + self-check: 10 / 10 passed on the first draft (no fix rounds)
     └─ shortcut check: tests of 4 / 10 tasks were beaten by the model-driven Hacker
         ├─ tests strengthened automatically, 4 times
         │    └─ 1 of the 4 fixes broke the reference solution  -> rejected (oracle)
         └─ difficulty (Qwen, 5 attempts, 5 more when 0/5 or 5/5)
              ├─ in range on the first measurement: 4 tasks        -> accepted
              └─ Qwen 0 / 10: 5 tasks; strong model passed all of them -> "truly hard"
                   └─ one repair each (all added information to the instruction)
                        ├─ landed in range: 2 (3/5, 1/10)          -> accepted
                        ├─ overshot to 10/10: 1                     -> dropped at H3
                        └─ still 0/10: 2                            -> dropped at H3
```

**Rejected after passing the oracle: 4 of 10 (40%).** Every proposal produced a task whose reference solution passed its tests at least once; four of them were still rejected by the other checks or by a person. An oracle-only pipeline would also have accepted four tasks whose tests a model could beat.

## Findings

1. **The model-driven Hacker found every weak test; the scripted shortcuts found none.** Across 18 hack checks the 7 scripted strategies never passed; the Opus Hacker (low effort, at most 10 turns, $0.012 per call) passed 4 times. The scripted list still guards the sandbox itself: every write outside `/app` was refused, as designed.
2. **A verifier fix can over-tighten.** One of four automatic test fixes made the tests fail the reference solution. The oracle gate caught it because every changed task goes through every check again (invariant 7).
3. **Qwen is the bottleneck for this pattern.** It passed 22 of 130 attempts (17%). The strong model passed all 7 of its diagnosis attempts. Half of the tasks were out of Qwen's reach on the first try.
4. **Repairs are hit or miss: 2 of 5 landed in range.** The one-change rule limits *where* a repair changes the task, not *how much*: one hint moved a task from 0/10 to 10/10.
5. **The partial-order check hardly filtered.** It kept 139 of 144 rubric items. With one strong attempt per task, the strong side's score is 0 or 1, so the check is weak and H1 is the real filter. More strong attempts per task would fix this and cost little (scoring is ~$0.05 per attempt).
6. **No refusals.** None of the 289 Opus calls was declined, the Hacker role included; no fallback model was ever used.
7. **The warm pool served 40% of sandboxes** (125 of 313). Hints arrive only after the oracle gate, so the Task Builder's self-check and the oracle gate always start cold.

## Where the money went

| Role | Paid calls | Cost | Note |
|---|---|---|---|
| Task Builder (10 builds + 4 test fixes) | 14 | $9.26 | $0.66 per call; 455k output tokens, mostly hidden thinking at effort high |
| Rubric scorer | 135 | $6.88 | Mining (one-time) |
| Qwen (weak model) | 3,980 | $4.03 | 130 attempts, $0.031 each; 23.6M input tokens: each turn re-sends the whole conversation |
| Rubric writer | 26 | $2.31 | Mining (one-time) |
| Blind Judge | 13 | $1.75 | |
| Leak checker | 15 | $0.76 | |
| Repairer | 5 | $0.73 | |
| Hacker | 50 | $0.61 | |
| Strong solver (diagnosis) | 29 | $0.58 | 7 attempts, all passed |
| Proposals + pattern grouping | 2 | $0.18 | |

Pattern mining (writer + scorer + grouping) cost **$9.28** and does not need repeating: a later run starts from the mined patterns (`run start --from-patterns`).

## What went wrong during the run, and what was done

| Time (ET) | What | Effect | Fix |
|---|---|---|---|
| 22:25 | SiliconFlow stopped serving the model on OpenRouter 25 minutes after the smoke test | 5 attempts failed; 1 task rejected (`rollouts_failed`) | Paused the run; switched to Amazon Bedrock before any Qwen attempt had succeeded (D-029); a missing host now makes callers wait instead of failing |
| 22:30 | The leak rule "hidden-test line in the workspace" rejected a task whose tests reuse lines of its *input* files | 1 task rejected wrongly | The rule became a flag for H4 (D-030) |
| 22:55 | A command printed a zero byte, which Postgres JSON cannot store | 1 attempt failed; its task rejected | Zero bytes shown as `\0` (D-032) |
| 22:58 | Qwen named a tool `finish</function`; Bedrock rejects any request whose history holds such a name | 1 attempt failed; its task rejected | Names cleaned in the history sent back (D-033) |

All four lost tasks were recovered inside the same run with the new operator action `reopen_branch` (D-031); three of them ended up accepted.

## Fix before P4

Items 1-4 were fixed on 2026-11-17 (decision D-034); 5 and 6 cost money and are in the P4 plan.

1. **A failed attempt should be retried, not reject its task.** `rollouts_failed` rejected three tasks in one night, each recovered by hand.
2. **Give the Repairer the target range and the current pass rate**, and ask for the smallest change that could get there.
3. **Make the Task Builder use hidden inputs that differ from the visible example.** One accepted task's hidden check for 2024-03-01 repeated the example exactly (other dates still made it a fair test).
4. **Warm-pool bookkeeping**: sandboxes whose step was killed mid-run stay counted as in use (14 of 48 slots after this run) until sandboxd restarts. The reap loop should drop ids whose container no longer exists.
5. **More strong attempts per task for the partial-order check** (finding 5).
6. Optional, to cut cost: Task Builder at effort medium instead of high. It is 40% of the per-task cost; whether build quality holds needs a small comparison first.

## What the pilot does not show

- **Human review time.** Every decision was entered with an estimated minute count rather than timed; "human minutes per accepted task" (2.67) is not a measurement.
- **Whether the tasks teach anything.** The pilot ends at an accepted task set; no model was trained on it.
- **Statistical stability.** One pattern, ten proposals: the shares above have wide uncertainty.

## For P4

At the measured unit cost, one proposal costs about $1.78 of Anthropic + Qwen after mining (10 proposals: $17.81). 100 proposals would be about **$180** and, at this pilot's rate, give about 60 accepted tasks; less if items 2 and 6 above cut the repair and build costs. P4 is not started: it needs its own plan and approval.
