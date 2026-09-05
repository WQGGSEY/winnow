#!/usr/bin/env python3
"""JSON-lines interface to the Lab3 game engine; supplies no agent policy."""
from __future__ import annotations

import json
from pathlib import Path
import random
import sys

import capture
from layout import Layout


class Simulator:
    def __init__(self):
        self.state = None
        self.turn = 0
        self.total_food = 0

    def reset(self, *, layout="defaultCapture", seed=0, max_moves=1200, starting_agent=None):
        if not isinstance(layout, str) or not layout.isidentifier():
            raise ValueError("layout must be a bundled layout name without extension")
        if type(seed) is not int or type(max_moves) is not int or max_moves <= 0:
            raise ValueError("seed must be an integer and max_moves a positive integer")
        if starting_agent is not None and (type(starting_agent) is not int or starting_agent not in (0, 1)):
            raise ValueError("starting_agent must be 0, 1, or null, matching Lab3 start rules")
        text = (Path(__file__).parent / "layouts" / f"{layout}.lay").read_text()
        board = Layout(text.splitlines())
        if len(board.agentPositions) != 4:
            raise ValueError("The simulator requires a four-agent layout")
        state = capture.GameState()
        state.initialize(board, 4)
        state.data.timeleft = max_moves
        self.state = state
        self.total_food = board.totalFood
        self.turn = random.Random(seed).randint(0, 1) if starting_agent is None else starting_agent
        return self.observe()

    def observe(self):
        if self.state is None:
            raise ValueError("reset is required")
        state = self.state
        return {
            "current_agent": None if state.isOver() else self.turn,
            "terminal": state.isOver(), "time_left": state.data.timeleft,
            "score_red": state.getScore(),
            "red_team": state.getRedTeamIndices(), "blue_team": state.getBlueTeamIndices(),
            "width": state.data.layout.width, "height": state.data.layout.height,
            "walls": state.getWalls().asList(), "food": state.data.food.asList(),
            "capsules": list(state.data.capsules),
            "agents": [{
                "index": i, "position": a.getPosition(), "direction": a.getDirection(),
                "start": a.start.getPosition(), "is_pacman": a.isPacman,
                "scared_timer": a.scaredTimer, "carrying": a.numCarrying,
                "returned": a.numReturned,
            } for i, a in enumerate(state.data.agentStates)],
            "legal_actions": [] if state.isOver() else state.getLegalActions(self.turn),
        }

    def step(self, *, agent, action):
        if self.state is None or self.state.isOver():
            raise ValueError("reset is required before stepping an absent or finished game")
        if type(agent) is not int or agent != self.turn:
            raise ValueError("action must come from current_agent")
        if action not in self.state.getLegalActions(agent):
            raise ValueError("action is not legal")
        before = self.state.getScore()
        # Upstream stores this per-layout rule as a module global.
        capture.TOTAL_FOOD = self.total_food
        self.state = self.state.generateSuccessor(agent, action)
        if self.state.data.timeleft == 0:
            self.state.data._win = True
        self.turn = (self.turn + 1) % 4
        result = self.observe()
        delta = self.state.getScore() - before
        result["reward"] = {"red": delta, "blue": -delta}
        return result


def main():
    simulator = Simulator()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            op = request.pop("op", None)
            if op not in {"reset", "observe", "step"}:
                raise ValueError("op must be reset, observe, or step")
            result = {"ok": True, "result": getattr(simulator, op)(**request)}
        except (ValueError, TypeError, OSError) as exc:
            result = {"ok": False, "error": str(exc)}
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
