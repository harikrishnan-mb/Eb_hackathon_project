"""Bounded ReAct state machine (design.md ADR-04, Gate 4, Gate 9.1).

States: INIT → TRIAGE → REASONING → TOOL_CALL → OBSERVATION → VERIFY → DONE,
with HALTED_* terminal states. Limits (proposed values, C5/C6):
  max_iterations   = 5
  total budget     = 380 ms (loop ceiling)
  per-step timeout = 120 ms (tool calls)
Recursive self-calls without a state change are impossible by construction:
each transition must be listed in TRANSITIONS.

Timeouts are measured after the fact. A synchronous tool call cannot be
pre-empted here. A step that exceeds its timeout is reported as E_TIMEOUT.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum

from .errors import ErrorCode, PlatformError


class State(str, Enum):
    INIT = "INIT"
    TRIAGE = "TRIAGE"
    REASONING = "REASONING"
    TOOL_CALL = "TOOL_CALL"
    OBSERVATION = "OBSERVATION"
    VERIFY = "VERIFY"
    DONE = "DONE"
    HALTED_BUDGET = "HALTED_BUDGET"
    HALTED_STEPS = "HALTED_STEPS"
    HALTED_BREAKER = "HALTED_BREAKER"


TERMINAL = {State.DONE, State.HALTED_BUDGET, State.HALTED_STEPS, State.HALTED_BREAKER}

TRANSITIONS: dict[State, set[State]] = {
    State.INIT: {State.TRIAGE},
    State.TRIAGE: {State.REASONING, State.DONE},
    State.REASONING: {
        State.TOOL_CALL, State.VERIFY, State.DONE,
        State.HALTED_STEPS, State.HALTED_BUDGET, State.HALTED_BREAKER,
    },
    State.TOOL_CALL: {State.OBSERVATION, State.HALTED_BREAKER, State.HALTED_BUDGET},
    State.OBSERVATION: {State.REASONING, State.VERIFY, State.HALTED_BUDGET, State.HALTED_STEPS},
    State.VERIFY: {State.DONE},
}


class ReActLoop:
    def __init__(
        self,
        *,
        max_iterations: int = 5,
        total_budget_ms: float = 380.0,
        per_step_timeout_ms: float = 120.0,
        breaker_threshold: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_iterations = max_iterations
        self.total_budget_ms = total_budget_ms
        self.per_step_timeout_ms = per_step_timeout_ms
        self.breaker_threshold = breaker_threshold
        self._clock = clock
        self.state = State.INIT
        self.iterations = 0
        self._started: float | None = None
        self._failures: dict[tuple[str, str], int] = {}

    # --- budget -----------------------------------------------------------
    def start(self) -> None:
        self._started = self._clock()

    def elapsed_ms(self) -> float:
        if self._started is None:
            return 0.0
        return (self._clock() - self._started) * 1000.0

    def remaining_ms(self) -> float:
        return self.total_budget_ms - self.elapsed_ms()

    # --- transitions ------------------------------------------------------
    def transition(self, to: State) -> None:
        if self.state in TERMINAL:
            raise PlatformError(ErrorCode.INVALID_REQUEST, f"loop already terminal ({self.state.value})")
        if to not in TRANSITIONS.get(self.state, set()):
            raise PlatformError(
                ErrorCode.INVALID_REQUEST,
                f"illegal transition {self.state.value} -> {to.value}",
            )
        self.state = to

    def begin_tool_call(self, tool: str, args_hash: str) -> bool:
        """Count an iteration and check limits. Returns False if the call must not run."""
        if self.iterations >= self.max_iterations:
            self.transition(State.HALTED_STEPS)
            return False
        if self.remaining_ms() <= 0:
            self.transition(State.HALTED_BUDGET)
            return False
        if self._failures.get((tool, args_hash), 0) >= self.breaker_threshold:
            self.transition(State.HALTED_BREAKER)
            return False
        self.iterations += 1
        self.transition(State.TOOL_CALL)
        return True

    def finish_tool_call(self, *, tool: str, args_hash: str, step_ms: float, ok: bool) -> None:
        """Record the outcome. Enforces the per-step timeout after the fact."""
        if not ok or step_ms > self.per_step_timeout_ms:
            key = (tool, args_hash)
            self._failures[key] = self._failures.get(key, 0) + 1
        if self.remaining_ms() <= 0:
            self.transition(State.HALTED_BUDGET)
            return
        self.transition(State.OBSERVATION)

    def breaker_open(self, tool: str, args_hash: str) -> bool:
        return self._failures.get((tool, args_hash), 0) >= self.breaker_threshold
