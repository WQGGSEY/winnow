# Lab3 game interface

This input contains the game engine, fixed maps, the upstream maze generator,
and a JSON-lines simulator. It
contains no agent policy, learner, checkpoint, baseline implementation, research
report, or proposed solution. Write the policies and training code yourself.

The game has four agents. Two teammates share a score and compete against the
other team. Observations expose the full board and all agent states. Agents act
in cyclic index order. Lab3 randomly starts with agent 0 or 1. Returning carried
food to home territory changes the team score. Captures score zero directly.
The original engine determines food, capsule, collision, and terminal rules.

Run `python simulator.py`. Send one JSON object per line. Each response is an
object containing `ok` and either `result` or `error`.

```json
{"op":"reset","layout":"defaultCapture","seed":0,"max_moves":1200}
{"op":"observe"}
```

To advance one move, send `{"op":"step","agent":INDEX,"action":ACTION}`,
using `current_agent` and one of `legal_actions` from the last observation.
There is no automatic opponent: the caller supplies every agent's action.
`reset` also accepts `starting_agent` equal to 0 or 1 for controlled starts.
`seed` only chooses this starting agent; different seed integers can produce the
same initial condition. It is not an independent source of game randomness.

To generate a map with the pinned upstream generator, use a positive
`layout_seed` instead of `layout`:

```json
{"op":"reset","layout_seed":12345,"starting_agent":0,"max_moves":1200}
```

The same layout seed reproduces the same map. Generation preserves the caller's
Python random state. A generated map is not automatically a valid holdout: its
sampling protocol and isolation from development still need to be established.
Coordinates have their origin at the bottom left. Walls, food, capsules, agent
positions, directions, carrying counts, returned counts, and scared timers are
included in observations. `reward.red` is the change in the signed red score;
`reward.blue` is its negative. No reward shaping is provided. At the move limit
or an engine terminal state, `terminal` is true and `current_agent` is null.
Invalid actions do not advance the state. The Python `Simulator` class exposes
the same reset, observe, and step operations for in-process simulation.

`input_manifest.json` records the exact input files, their hashes, the upstream
revision, and the hashes of the original game sources. The extracted capture
module excludes the old CLI, policy loader, keyboard agents, and score-export
dependencies. Game transition rules are retained. No learning result is supplied
or claimed by this package.

The engine originated in the UC Berkeley Pacman AI projects,
<http://ai.berkeley.edu>, and was adapted by WQGGSEY/Lab3. Original attribution
and licensing notices are retained in the engine files.
