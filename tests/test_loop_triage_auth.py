import pytest

from assistant.auth import DevHeaderAuthenticator
from assistant.errors import ErrorCode, PlatformError
from assistant.loop import ReActLoop, State
from assistant.triage import classify, route_safely


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance_ms(self, ms):
        self.t += ms / 1000.0


def test_loop_happy_path_reaches_done():
    loop = ReActLoop()
    loop.start()
    loop.transition(State.TRIAGE)
    loop.transition(State.REASONING)
    assert loop.begin_tool_call("hybrid_retrieve", "h1")
    loop.finish_tool_call(tool="hybrid_retrieve", args_hash="h1", step_ms=10, ok=True)
    loop.transition(State.REASONING)
    loop.transition(State.VERIFY)
    loop.transition(State.DONE)
    assert loop.state == State.DONE


def test_loop_halts_at_max_iterations():
    loop = ReActLoop(max_iterations=2)
    loop.start()
    loop.transition(State.TRIAGE)
    loop.transition(State.REASONING)
    for i in range(2):
        assert loop.begin_tool_call("t", f"h{i}")
        loop.finish_tool_call(tool="t", args_hash=f"h{i}", step_ms=1, ok=True)
        loop.transition(State.REASONING)
    assert not loop.begin_tool_call("t", "h9")
    assert loop.state == State.HALTED_STEPS


def test_loop_halts_on_total_budget():
    clock = FakeClock()
    loop = ReActLoop(total_budget_ms=380, clock=clock)
    loop.start()
    loop.transition(State.TRIAGE)
    loop.transition(State.REASONING)
    clock.advance_ms(381)
    assert not loop.begin_tool_call("t", "h")
    assert loop.state == State.HALTED_BUDGET


def test_circuit_breaker_trips_after_two_identical_failures():
    loop = ReActLoop(breaker_threshold=2)
    loop.start()
    loop.transition(State.TRIAGE)
    loop.transition(State.REASONING)
    for _ in range(2):
        assert loop.begin_tool_call("t", "same")
        loop.finish_tool_call(tool="t", args_hash="same", step_ms=1, ok=False)
        loop.transition(State.REASONING)
    assert not loop.begin_tool_call("t", "same")
    assert loop.state == State.HALTED_BREAKER


def test_illegal_transition_rejected():
    loop = ReActLoop()
    with pytest.raises(PlatformError) as exc:
        loop.transition(State.DONE)
    assert exc.value.code == ErrorCode.INVALID_REQUEST


def test_terminal_state_cannot_move():
    loop = ReActLoop()
    loop.transition(State.TRIAGE)
    loop.transition(State.DONE)
    with pytest.raises(PlatformError):
        loop.transition(State.REASONING)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Approve purchase order 7", "TRANSACTION_EXEC"),
        ("What is the returns policy?", "HYBRID_RAG"),
        ("hello!", "DIRECT_ANSWER"),
        ("write me a poem", "REJECT_OOS"),
    ],
)
def test_triage_routes(query, expected):
    assert route_safely(classify(query)) == expected


def test_low_confidence_never_reaches_transaction_exec():
    from assistant.triage import TriageResult

    low = TriageResult("workflow_change", "operations", "TIER_3", "high", "TRANSACTION_EXEC", 0.5)
    assert route_safely(low) == "HYBRID_RAG"
    not_tier3 = TriageResult("x", "operations", "TIER_1", "high", "TRANSACTION_EXEC", 0.99)
    assert route_safely(not_tier3) == "HYBRID_RAG"


def test_dev_authenticator_reads_tenant_from_headers():
    auth = DevHeaderAuthenticator(env={"APP_ENV": "dev"})
    p = auth.authenticate({"x-tenant-id": "tenant_a", "x-user-id": "u1"})
    assert (p.tenant_id, p.user_id) == ("tenant_a", "u1")
    with pytest.raises(PlatformError):
        auth.authenticate({"x-tenant-id": "bad", "x-user-id": "u1"})


def test_dev_authenticator_refuses_production():
    with pytest.raises(RuntimeError):
        DevHeaderAuthenticator(env={"APP_ENV": "production"})
