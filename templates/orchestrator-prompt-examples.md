# Orchestrator Prompt Examples (for Bob)

Use these (or adapt) when directing the Engineer bot.

## Kick off a focused loop
Talk to the ORB Engineer bot and have them loop every 10 minutes on building the next concrete piece of the walk-forward engine in the orb-walk-forward repo. Keep them on target for [specific goal, e.g. "a minimal ORB range + breakout signal module that loads sample data and emits signals"]. Every 45 minutes review their progress and suggest exactly one improvement to get closer to a usable walk-forward skeleton. Do not expand scope. Report status back to me.

## Single next step
Instruct the ORB Engineer to implement [specific function or file] in the orb-walk-forward repo. Have them open a PR or commit when the unit is working. Then summarize what was done and the next logical step.

## Review and refine
Review the latest work by the ORB Engineer in the orb-walk-forward repo. Identify the single highest-leverage improvement for robustness or clarity of the walk-forward process. Instruct them to make only that change next.

## Boundaries reminder (include as needed)
Remind the Engineer: research and code only. No live orders, no credential handling, no changes outside this repo without explicit approval via you.
