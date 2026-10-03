# Adaptive planning

An agent skill for tasks where each result can change what should happen next.
Keep the objective stable, execute one meaningful step, verify the result,
and reassess the remaining plan before continuing.

The skill includes an optional local dashboard that shows dependencies,
current work, evidence, retired steps, and revision history. Updates arrive
through a live event stream whenever the agent publishes a new snapshot.

## How it works

```text
Plan -> Choose -> Execute one step -> Observe -> Reassess -> Report
  ^                                               |
  +---------- update the remaining plan -----------+

Goal: verified report

[x] Inspect inputs
    |
    +--> [>] Filter sales --+
    |                      |
    +--> [ ] Check lookup --+--> [ ] Join --> [ ] Verify report
```

Choose the execution scope in your request:

| Mode | Agent behavior |
| --- | --- |
| Plan only | Inspect without changing the target, produce the plan and requested plan files, then stop. |
| First step | Execute and verify one step, reassess, then stop. |
| Complete the task | Continue through authorized work until the objective is verified or a stopping condition applies. |

A blocked branch does not halt independent authorized work. Failed checks stay
visible, and completing a tool call does not by itself complete a step.

## Install

Clone the skill into your agent's skills directory. These examples use
`.agents/skills`, with `SKILL.md` at the root of the installed skill.

Windows PowerShell:

```powershell
$skillDirectory = Join-Path $env:USERPROFILE '.agents\skills\adaptive-planning'
git clone https://github.com/arduinitavares/adaptive-planning.git $skillDirectory
```

macOS or Linux shell:

```sh
git clone https://github.com/arduinitavares/adaptive-planning.git \
  "$HOME/.agents/skills/adaptive-planning"
```

Use an empty destination. If a copy is already installed, review its local
changes before replacing or updating it. A development checkout can live
elsewhere; the agent must be able to discover or read its `SKILL.md`.

Ordinary planning needs only the Markdown instructions. The dashboard requires
Python 3.10+ and a browser. It uses the Python standard library and local assets,
with no package installation or hosted service.

## Use

In an agent that supports named skills, ask:

```text
Use $adaptive-planning to plan this migration. Inspect what you need,
but stop after producing the plan.
```

```text
Use $adaptive-planning. Execute the first step, verify it,
update the plan, and stop.
```

```text
Use $adaptive-planning to complete this task. Keep a live HTML dashboard
updated as you execute and verify each step.
```

You can also give the agent the path to [SKILL.md](SKILL.md). The dashboard is
optional. Its [reference](references/live-dashboard.md) covers launch commands,
proposal JSON, publishing, authorization intervals, migration, and recovery.

Checkpoints use concise prose and ASCII dependency maps. If the optional
`caveman` and `pstack-plugin:unslop` writing skills are installed, the skill uses
them for task reporting. Otherwise it uses short, direct sentences with the
same evidence and scope boundaries.

## Dashboard boundaries

The server binds to `127.0.0.1` and serves a read-only view. Keep its state in
the current task's directory, outside the shared skill installation. Publish
updates through `scripts/live_plan.py`; do not edit canonical state by hand.

Schema v2 checks dependency order, generation and revision conflicts,
append-only evidence, retirement, and execution scope. A first-step interval
retains its claimed step even if that attempt becomes blocked. Scope changes
require a recorded authorization note grounded in the user's instructions.

The publisher checks consistency. The agent remains responsible for whether
permission was actually given and whether evidence proves a result. Evidence
has no structured pass/fail verdict yet. Historical completed steps remain
trusted by the publisher; if later evidence disproves a result, the agent must
preserve that history, add corrective work, and report the invalidation clearly.

In plan-only mode, a finished plan retains status `planning` and records
"Plan ready; execution has not started" in its change summary.

## Verification

From the repository root, with Python and Node.js available:

```sh
python -B -m unittest discover -s tests -p "test_*.py" -v
node --check assets/dashboard/app.js
node --check tests/test_frontend_dom.js
```

The suite covers publishing, scope accounting, migration, recovery, server
behavior, and frontend event handling. Node.js runs the frontend checks.
Windows-specific checks use real file handles; symlink checks can skip when
the operating system denies symlink creation. Tests create their own temporary
state and servers.

The initial release was verified on Windows with Python 3.13.3 and Node.js
22.14.0: 63 tests, 61 passed and 2 skipped for symlink privileges. Browser
checks covered live updates, narrow screens, literal text rendering, and
reduced motion. macOS, Linux, screen readers, and long-running streams have
not been verified.

## Inspiration

The original idea came from reinforcement learning: an action changes the
state, so the next decision should use what was actually observed. The
[University of Alberta's Reinforcement Learning Specialization](https://www.ualberta.ca/en/admissions-programs/online-courses/reinforcement-learning/index.html)
and Sutton and Barto's
[Reinforcement Learning: An Introduction](https://mitpress.ublish.com/book/reinforcement-learning-an-introduction-2)
provide the background for that intuition.

This skill implements closed-loop, receding-horizon replanning. It does not
learn value functions across tasks, estimate rewards, or tune a discount
factor. Keeping distant steps coarse is a planning choice. The university and
authors are cited as inspiration, with no affiliation or endorsement implied.

## License

[MIT](LICENSE).
