from host.core.clock import ManualClock
from host.core.config import InitiativeConfig
from host.core.initiative import InitiativeGate


def make(**kw):
    clock = ManualClock()
    return InitiativeGate(InitiativeConfig(**kw), clock), clock


def test_allows_up_to_limit_then_refuses():
    gate, _ = make(max_per_hour=2)
    assert gate.acquire() and gate.acquire()
    assert not gate.allowed()
    assert not gate.acquire()


def test_window_slides_over_60_minutes():
    gate, clock = make(max_per_hour=2)
    gate.acquire()
    clock.advance(1800)
    gate.acquire()
    clock.advance(1799)
    assert not gate.allowed()  # pierwsza zgoda ma 3599 s
    clock.advance(1)
    assert gate.allowed()  # 3600 s: wypadła z okna
    assert gate.acquire()
    assert not gate.allowed()


def test_quiet_blocks_everything():
    gate, _ = make(max_per_hour=5, quiet=True)
    assert not gate.allowed() and not gate.acquire()
    gate.quiet = False
    assert gate.acquire()


def test_refused_acquire_is_not_recorded():
    gate, clock = make(max_per_hour=1)
    gate.acquire()
    clock.advance(10)
    gate.acquire()  # odmowa
    clock.advance(3590)
    assert gate.allowed()  # liczy się tylko pierwsza zgoda


def test_zero_limit_never_allows():
    gate, _ = make(max_per_hour=0)
    assert not gate.allowed()
