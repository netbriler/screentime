from datetime import UTC, datetime, timedelta

from screentime import session

T0 = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_record_opens_and_closes_a_stretch():
    away: session.Away = []
    assert not session.record(away, at(0), True)
    assert session.record(away, at(1), False)
    assert not session.record(away, at(2), False)
    assert away == [[at(1).isoformat(), None]]
    assert not session.record(away, at(30), None)  # unknown changes nothing
    assert session.record(away, at(31), True)
    assert away == [[at(1).isoformat(), at(31).isoformat()]]


def test_old_stretches_are_forgotten():
    away: session.Away = [[at(0).isoformat(), at(5).isoformat()]]
    assert session.record(away, at(0) + session.KEEP + timedelta(minutes=10), True)
    assert away == []


def test_sample_writes_only_changes(tmp_path, monkeypatch):
    path = tmp_path / "away.json"
    monkeypatch.setattr(session, "on_console", lambda: False)
    session.sample(path, at(0))
    session.sample(path, at(1))
    assert session.load(path) == [[at(0).isoformat(), None]]
    monkeypatch.setattr(session, "on_console", lambda: True)
    session.sample(path, at(2))
    assert session.load(path) == [[at(0).isoformat(), at(2).isoformat()]]


def test_in_background_needs_a_minute_of_overlap():
    away = [[at(10).isoformat(), at(70).isoformat()]]
    assert session.in_background(at(0), at(80), away, at(90))  # the app left in front the whole time
    assert not session.in_background(at(0), at(10.5), away, at(90))  # switched out right after
    assert not session.in_background(at(70), at(80), away, at(90))  # after coming back


def test_open_stretch_lasts_until_now():
    away = [[at(10).isoformat(), None]]
    assert session.in_background(at(0), at(20), away, at(20))
