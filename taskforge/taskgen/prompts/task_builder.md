Build a complete terminal task from the proposal below, in Harbor layout. The task targets this agent weakness: [$category] $pattern

PROPOSAL: $title
$statement

Files to produce (paths are relative to the task root):
- instruction.md: everything the agent is told. State every rule the tests check: exact paths, input and output formats, edge cases.
- task.toml: metadata with a [metadata] table containing difficulty and tags.
- environment/Dockerfile: FROM python:3.12-slim; install pytest; COPY app/ /app/. No other network use at run time.
- environment/app/...: the starting workspace. Include a small sample input with its expected output so the agent can check its own work. The sample must not cover every case the hidden tests check.
- tests/test.sh: runs the hidden tests and exits 0 only if they all pass. Use exactly:
    cd /tests && python3 -I -m pytest -q -p no:cacheprovider --rootdir=/tests test_outputs.py
- tests/test_outputs.py: hidden tests. They must call the agent's program in a subprocess (never import it), check values rather than only file existence, and run in under 60 seconds with the standard library and pytest only. Every hidden test case must use inputs that differ from the visible sample, and must not reuse its expected output: a hidden check that repeats the visible example tests nothing, because the agent can copy the example.
- solution/solve.sh: a bash script that solves the task from the starting state, writing into /app.

The tests run as an unprivileged user in a fresh container that receives only /app. Hidden test data must not appear anywhere in /app or in the instruction.

Reply with only a JSON object mapping each path to its full text:
{"files": {"instruction.md": "...", "task.toml": "...", "environment/Dockerfile": "...", "environment/app/...": "...", "tests/test.sh": "...", "tests/test_outputs.py": "...", "solution/solve.sh": "..."}}
