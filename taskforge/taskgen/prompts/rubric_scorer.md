Score one attempt at a terminal task against each rubric item below. You see only the commands the agent ran and their outputs. You are not told whether the attempt succeeded; do not guess it, and do not let a guess change any score.

Score each item 1 if its rule is clearly met by what is visible in the attempt, otherwise 0. When in doubt, 0.

TASK
$task

RUBRIC ITEMS
$items

ATTEMPT
$trajectory

Reply with only a JSON object mapping item numbers to scores:
{"scores": {"1": 0, "2": 1}}
