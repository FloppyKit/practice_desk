"""Visit windows. A gap is a second window. No network."""
from datetime import date
from zoneinfo import ZoneInfo

from app.slots import window_starts, windows_for


def _hm(starts):
    return [s.strftime("%H:%M") for s in starts]


def test_new_client_last_start_is_4pm() -> None:
    tz = ZoneInfo("America/Chicago")
    starts = window_starts(
        date(2026, 10, 12),
        {"start": "09:00", "end": "18:00", "latest_start": "16:00"},
        interval=30,
        cal_dur=120,
        tz=tz,
    )
    hours = _hm(starts)
    assert hours[0] == "09:00"
    assert hours[-1] == "16:00"
    assert "16:30" not in hours


def test_follow_up_gaps() -> None:
    et = {
        "hours_by_duration": {
            "30": {
                "monday": {
                    "windows": [
                        {"start": "09:30", "end": "13:00"},
                        {"start": "15:00", "end": "18:00"},
                    ]
                }
            },
            "60": {
                "monday": {
                    "windows": [
                        {"start": "09:30", "end": "13:00"},
                        {"start": "14:00", "end": "18:00"},
                    ]
                }
            },
        }
    }
    day = date(2026, 10, 12)
    tz = ZoneInfo("America/Chicago")
    short = []
    for window in windows_for(et, "monday", 30):
        short.extend(window_starts(day, window, interval=30, cal_dur=30, tz=tz))
    long = []
    for window in windows_for(et, "monday", 60):
        long.extend(window_starts(day, window, interval=30, cal_dur=60, tz=tz))
    assert _hm(short)[0] == "09:30"
    assert "12:30" in _hm(short)
    assert "13:00" not in _hm(short)
    assert "15:00" in _hm(short)
    assert _hm(short)[-1] == "17:30"
    assert "12:00" in _hm(long)
    assert "12:30" not in _hm(long)
    assert "14:00" in _hm(long)
    assert _hm(long)[-1] == "17:00"
    assert windows_for(et, "friday", 30) == []
