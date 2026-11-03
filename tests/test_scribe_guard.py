from datetime import datetime, timedelta, timezone

from app.visit_link import multi_visit


def _v(h, m, mins):
    s = datetime(2026, 11, 2, h, m, tzinfo=timezone.utc)
    return {"event_id": f"e{h}{m}", "start": s, "end": s + timedelta(minutes=mins), "client_id": ""}


DAY = [_v(15, 0, 30), _v(15, 30, 30), _v(16, 0, 30), _v(16, 30, 60), _v(17, 30, 30)]


def test_single_visit_with_overrun_is_not_multi():
    s = datetime(2026, 11, 2, 15, 1, tzinfo=timezone.utc)
    assert not multi_visit(s, s + timedelta(minutes=36), DAY)


def test_long_multi_patient_recording_is_multi():
    s = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    assert multi_visit(s, s + timedelta(minutes=195), DAY)


def test_longer_than_visit_plus_30_is_multi():
    s = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    assert multi_visit(s, s + timedelta(minutes=70), [_v(15, 0, 30)])
