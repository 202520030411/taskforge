You design terminal tasks for training and evaluating coding agents. Each task must give an agent a fair chance to show a specific weakness, so that an agent that has it fails and an agent without it passes.

WEAKNESS TO TARGET
Category: $category
$description

Write $n task proposals. Each proposal is a short paragraph a task author could build from: the scenario, what the agent must produce, and why an agent with this weakness would likely get it wrong while a careful agent gets it right.

Requirements:
- Self-contained: Debian with Python 3.12 and common command-line tools; no network after setup.
- Graded from files the agent leaves in /app (no long-running servers).
- Realistic work that takes a competent engineer 10-30 minutes, not a puzzle.
- Every rule the grader checks must be stated in the instruction. Difficulty must come from what careful work catches (edge cases stated in the spec, checking one's own output, recovering from a failed step), never from hidden information.
- Do not base a proposal on any existing public benchmark task.
- Be different from these existing proposals:
$avoid

Reply with only a JSON object:
{"proposals": [{"title": "...", "statement": "..."}]}
