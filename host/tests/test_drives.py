from datetime import datetime, timedelta, timezone

from host.core.body import BodyState
from host.core.clock import ManualClock
from host.core.config import Config
from host.core.drives import DriveEngine
from host.tests.helpers import heartbeat, make_event


class Rig:
    def __init__(self, config: Config | None = None, wall: datetime | None = None) -> None:
        self.cfg = config or Config()
        self.clock = ManualClock(wall)
        self.body = BodyState(self.cfg, self.clock)
        self.engine = DriveEngine(self.cfg, self.clock)
        self.last_interaction = self.clock.wall()

    def step(self, dt: float = 0, *, batt=None, temp=None, beat=True) -> list[tuple[str, str]]:
        """Przesuwa zegar, podaje odczyty i zwraca (typ, drive) zdarzeń."""
        self.clock.advance(dt)
        if batt is not None or beat:
            self.body.apply(heartbeat(batt if batt is not None else 71))
        if temp is not None:
            self.body.apply(make_event("body.sensor", {"temp_c": temp}))
        return [(e.type, e.data["drive"]) for e in self.engine.update(self.body, self.last_interaction)]

    def lose_link(self) -> None:
        self.clock.advance(5.5)
        self.body.tick()
        self.engine.update(self.body, self.last_interaction)


def only(events, drive):
    return [e for e in events if e[1] == drive]


ENTER_LOW = ("drive.enter", "battery_low")
EXIT_LOW = ("drive.exit", "battery_low")


# --- battery_low ---


def test_battery_low_enters_after_hold():
    r = Rig()
    assert r.step(0, batt=19) == []
    assert r.step(9, batt=19) == []
    assert r.step(1, batt=19) == [ENTER_LOW]
    assert r.engine.active() == ["battery_low"]


def test_battery_low_threshold_is_strict():
    r = Rig()
    r.step(0, batt=20)
    assert r.step(60, batt=20) == []
    assert r.engine.active() == []


def test_battery_low_timer_resets_when_condition_breaks():
    r = Rig()
    r.step(0, batt=19)
    r.step(8, batt=19)
    r.step(1, batt=21)  # przerwanie
    r.step(1, batt=19)  # nowy start
    assert r.step(9, batt=19) == []
    assert r.step(1, batt=19) == [ENTER_LOW]


def test_battery_low_exit_needs_25_for_30s():
    r = Rig()
    r.step(0, batt=19)
    r.step(10, batt=19)
    assert r.engine.active() == ["battery_low"]
    r.step(1, batt=25)
    assert r.step(29, batt=25) == []
    assert r.step(1, batt=25) == [EXIT_LOW]
    assert r.engine.active() == []


def test_battery_low_hysteresis_zone_keeps_state():
    r = Rig()
    r.step(0, batt=19)
    r.step(10, batt=19)
    assert r.step(600, batt=24) == []  # 20-24: ani wyjście, ani zerowanie stanu
    assert r.engine.active() == ["battery_low"]


def test_battery_low_exit_timer_resets_in_zone():
    r = Rig()
    r.step(0, batt=19)
    r.step(10, batt=19)
    r.step(1, batt=26)
    r.step(20, batt=26)
    r.step(1, batt=24)  # wejście w strefę zeruje licznik wyjścia
    r.step(1, batt=26)
    assert r.step(29, batt=26) == []
    assert r.step(1, batt=26) == [EXIT_LOW]


def test_battery_low_is_not_reported_twice_while_active():
    r = Rig()
    r.step(0, batt=19)
    r.step(10, batt=19)
    r.step(0, batt=5)
    assert r.step(10, batt=5) == [("drive.enter", "battery_critical")]
    assert r.step(100, batt=5) == []


# --- battery_critical ---


def test_critical_and_low_are_independent_and_ordered():
    r = Rig()
    r.step(0, batt=9)
    events = r.step(10, batt=9)
    assert events == [ENTER_LOW, ("drive.enter", "battery_critical")]
    assert r.engine.active() == ["battery_low", "battery_critical"]


def test_critical_exits_before_low():
    r = Rig()
    r.step(0, batt=9)
    r.step(10, batt=9)
    r.step(1, batt=16)
    assert r.step(30, batt=16) == [("drive.exit", "battery_critical")]
    assert r.engine.active() == ["battery_low"]
    r.step(1, batt=26)
    assert r.step(30, batt=26) == [EXIT_LOW]


# --- temp_high ---


def test_temp_high_enter_and_exit():
    r = Rig()
    r.step(0, temp=40)
    assert r.step(60, temp=40) == []  # 40 nie jest > 40
    r.step(0, temp=41)
    assert r.step(29, temp=41) == []
    assert r.step(1, temp=41) == [("drive.enter", "temp_high")]
    r.step(0, temp=37)  # 37 nie jest < 37
    assert r.step(120, temp=37) == []
    r.step(0, temp=36.9)
    assert r.step(59, temp=36.9) == []
    assert r.step(1, temp=36.9) == [("drive.exit", "temp_high")]


def test_temp_drive_freezes_when_temp_sensor_down():
    r = Rig()
    r.step(0, temp=41)
    r.step(20, temp=41)
    r.body.apply(make_event("body.fault", {"sensor": "temp", "code": 1}))
    assert r.step(60, temp=41) == []  # dane niedostępne: bez zmiany stanu
    r.body.apply(make_event("body.fault.clear", {"sensor": "temp"}))
    r.step(0, temp=41)
    assert r.step(29, temp=41) == []  # licznik startował od nowa
    assert r.step(1, temp=41) == [("drive.enter", "temp_high")]


# --- niedostępność danych (link) ---


def test_battery_timer_restarts_after_link_loss():
    r = Rig()
    r.step(0, batt=19)
    r.step(8, batt=19)
    r.lose_link()
    r.step(0, batt=19)  # łącze wróciło
    assert only(r.step(9, batt=19), "battery_low") == []
    assert only(r.step(1, batt=19), "battery_low") == [ENTER_LOW]


def test_active_battery_drive_does_not_exit_on_stale_data():
    r = Rig()
    r.step(0, batt=19)
    r.step(10, batt=19)
    r.step(1, batt=30)
    r.lose_link()
    assert r.engine.active() == ["battery_low", "link_lost"]
    # po powrocie łącza licznik wyjścia liczy się od nowa
    r.step(0, batt=30)
    assert only(r.step(29, batt=30), "battery_low") == []
    assert only(r.step(1, batt=30), "battery_low") == [EXIT_LOW]


# --- link_lost ---


def test_link_lost_enters_immediately_and_exits_after_5s_of_heartbeats():
    r = Rig()
    r.step(0)
    r.clock.advance(5.5)
    r.body.tick()
    assert r.step(0, beat=False) == [("drive.enter", "link_lost")]
    r.step(0)  # pierwszy heartbeat po utracie
    assert r.step(4, ) == []
    assert r.step(1) == [("drive.exit", "link_lost")]


def test_link_lost_recovery_timer_resets_on_new_loss():
    r = Rig()
    r.step(0)
    r.lose_link()
    assert r.engine.active() == ["link_lost"]
    r.step(0)
    r.step(3)
    r.lose_link()  # kolejna utrata
    r.step(0)
    assert r.step(4) == []
    assert r.step(1) == [("drive.exit", "link_lost")]


def test_link_unknown_does_not_activate_link_lost():
    r = Rig()
    r.clock.advance(3)
    assert [e.type for e in r.engine.update(r.body, r.last_interaction)] == []


# --- long_silence ---


def test_long_silence_enters_after_4h_inside_active_hours():
    r = Rig(wall=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert r.step(4 * 3600) == []  # dokładnie 4 h to nie "> 4 h"
    assert r.step(1) == [("drive.enter", "long_silence")]
    assert r.engine.active() == ["long_silence"]


def test_long_silence_waits_for_active_hours():
    r = Rig(wall=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))  # 00:00
    assert r.step(5 * 3600) == []  # 05:00, poza 08:00-22:00
    assert r.step(3 * 3600 - 1) == []  # 07:59:59
    assert r.step(1) == [("drive.enter", "long_silence")]  # 08:00:00


def test_long_silence_exits_on_interaction():
    r = Rig(wall=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
    r.step(4 * 3600 + 1)
    r.last_interaction = r.clock.wall()
    assert r.step(0) == [("drive.exit", "long_silence")]
    assert r.step(3600) == []


def test_long_silence_stays_active_after_active_hours_end():
    r = Rig(wall=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
    r.step(4 * 3600 + 1)
    r.step(12 * 3600)  # po 22:00
    assert r.engine.active() == ["long_silence"]


def test_long_silence_uses_local_time_of_wall_clock():
    tz = timezone(timedelta(hours=9))
    r = Rig(wall=datetime(2026, 1, 1, 2, 0, tzinfo=tz))  # 02:00 lokalnie = 17:00 UTC
    assert r.step(4 * 3600 + 1) == []  # 06:00 lokalnie (poza godzinami), 21:00 UTC (w godzinach)
    assert r.step(2 * 3600) == [("drive.enter", "long_silence")]  # 08:00 lokalnie


# --- konfiguracja i kolejność ---


def test_thresholds_come_from_config():
    cfg = Config.model_validate(
        {"drives": {"battery_low": {"direction": "below", "enter": 50, "exit": 60,
                                    "enter_hold_s": 2, "exit_hold_s": 3}}}
    )
    r = Rig(cfg)
    r.step(0, batt=45)
    assert r.step(2, batt=45) == [ENTER_LOW]


def test_active_list_follows_spec_order():
    r = Rig(wall=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
    r.step(0, batt=9, temp=41)
    r.step(30, batt=9, temp=41)
    r.step(4 * 3600, batt=9, temp=41)
    assert r.engine.active() == ["battery_low", "battery_critical", "temp_high", "long_silence"]
