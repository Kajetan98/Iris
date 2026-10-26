from datetime import datetime, timedelta, timezone

from host.core.continuity import Continuity

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def test_first_run_has_no_downtime():
    c = Continuity.at_start(NOW, None)
    assert c.last_downtime_s == 0
    assert c.sessions == 1
    assert c.last_restart == NOW


def test_downtime_is_gap_since_last_event():
    c = Continuity.at_start(NOW, NOW - timedelta(hours=3, seconds=7), prior_sessions=4, known_facts=12)
    assert c.last_downtime_s == 3 * 3600 + 7
    assert c.snapshot() == {"sessions": 5, "known_facts": 12}


def test_negative_gap_is_clamped_to_zero():
    assert Continuity.at_start(NOW, NOW + timedelta(minutes=5)).last_downtime_s == 0
