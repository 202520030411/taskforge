The hidden tests of a terminal task can be passed without solving it. The shortcuts below made the tests pass. Change only files under tests/ so that every one of these shortcuts fails, while a real solution of the task as stated in the instruction still passes. Do not add requirements that the instruction does not state.

INSTRUCTION
$instruction

CURRENT TESTS (path -> content)
$tests

SHORTCUTS THAT PASSED
$shortcuts

Reply with only a JSON object containing the test files you change, each with its full new text:
{"files": {"tests/test_outputs.py": "..."}}
