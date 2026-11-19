Check whether a terminal task leaks its answer to the agent. VISIBLE is what the agent sees (instruction and starting files). HIDDEN is the grader and the reference solution, which the agent never sees.

The task leaks if an agent could pass by copying something from VISIBLE instead of doing the work: the solution's code, the hidden test inputs or expected outputs, or anything from which they follow directly. A visible sample with its own expected output is allowed when the hidden tests use different inputs.

VISIBLE
$visible

HIDDEN
$hidden

Reply with only a JSON object:
{"suspect": false, "reason": "..."}
