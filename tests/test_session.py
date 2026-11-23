from datetime import UTC, datetime, timedelta

from screentime import session

T0 = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_record_opens_and_closes_a_stretch():
    state: dict = {}
    session.record(state, at(0), True)
    assert state["away"] == []
    session.record(state, at(1), False)
    session.record(state, at(2), False)
    assert state["away"] == [[at(1).isoformat(), None]]
    session.record(state, at(30), None)  # unknown changes nothing
    session.record(state, at(31), True)
    assert state["away"] == [[at(1).isoformat(), at(31).isoformat()]]


def test_old_stretches_are_forgotten():
    state = {"away": [[at(0).isoformat(), at(5).isoformat()]]}
    session.record(state, at(0) + session.KEEP + timedelta(minutes=10), True)
    assert state["away"] == []


def test_in_background_needs_a_minute_of_overlap():
    away = [[at(10).isoformat(), at(70).isoformat()]]
    assert session.in_background(at(0), at(80), away, at(90))  # the app left in front the whole time
    assert not session.in_background(at(0), at(10.5), away, at(90))  # switched out right after
    assert not session.in_background(at(70), at(80), away, at(90))  # after coming back


def test_open_stretch_lasts_until_now():
    away = [[at(10).isoformat(), None]]
    assert session.in_background(at(0), at(20), away, at(20))
