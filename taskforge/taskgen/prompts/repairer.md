A terminal task's difficulty is out of range for the weak agent it targets. Make exactly ONE change to fix that, inside ONE area of the package: instruction (instruction.md), environment (environment/...), tests (tests/...), or solution (solution/...).

The weak agent passed $passed of $attempts attempts. A good task is one it passes between $low and $high times out of $out_of: it sometimes succeeds and sometimes fails. Make the SMALLEST change that could move it into that range. Overshooting is a failure too: a hint that names the method, the commands or the trick turns a task it never solves into one it always solves.

DIAGNOSIS: $diagnosis
- too_easy: make it harder, using only one of these legitimate sources of difficulty:
$legal
- broken: fix what is wrong (clarify an ambiguous rule, relax a test that is stricter than the instruction, repair the environment).
- truly_hard: remove one step, or add one piece of necessary information.
Never make a task harder by hiding information, adding strange formatting demands, or making the environment unreliable.

HOW PEOPLE FIXED SIMILAR TASKS BEFORE
$examples

EVIDENCE
$evidence

PACKAGE (path -> content)
$package

Reply with only a JSON object; "files" holds only the files you change, with their full new text:
{"mutation": "a short name for the single change", "area": "tests", "files": {"tests/test_outputs.py": "..."}}
