Several attempts by the same coding agent at one terminal task are shown below: only the commands it ran and their outputs. You are not told whether any attempt passed.

Judge the TASK, not the agent. Decide which of these fits best:
- reasonable: the instruction is clear and complete, and a careful engineer could solve it in the given environment.
- unreasonable: the instruction is ambiguous, contradicts itself, depends on information the agent cannot obtain, or the environment is broken.
- no_good_solution: the task as stated seems to have no correct solution.
- cannot_judge: the attempts do not show enough to tell.

INSTRUCTION
$instruction

ATTEMPTS
$attempts

Reply with only a JSON object:
{"verdict": "reasonable", "evidence": "the specific commands or outputs your verdict rests on"}
