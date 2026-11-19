You are comparing two attempts at the same terminal task by coding agents. The STRONG attempt passed the hidden tests. The WEAK attempt failed them. You see only the commands each agent ran and the output it got back.

Find the points where the two attempts differ in a way that plausibly explains the outcome: what the strong agent did at some point, and what the weak agent did (or failed to do) at the corresponding point. Turn each into a rubric item that can be scored yes/no on any attempt at this task.

Rules for items:
- Each item describes a behaviour, not an outcome ("ran the program on the sample input and compared the output before finishing", not "got the right answer").
- An item must be scorable from commands and outputs alone.
- Do not mention which attempt passed, and do not refer to "the strong agent" or "the weak agent" inside an item.
- Prefer behaviours that would matter on other tasks too (checking work, reading the specification, handling errors) over task trivia.
- Write between 3 and $n items.

A reviewer deleted these earlier items as unhelpful. Do not write items like them:
$avoid

TASK
$task

STRONG ATTEMPT (passed)
$strong

WEAK ATTEMPT (failed)
$weak

Reply with only a JSON object:
{"items": [{"description": "...", "scoring_rule": "score 1 if ..., otherwise 0"}]}
