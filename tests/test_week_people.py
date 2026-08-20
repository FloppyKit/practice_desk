"""Week-people search — no calendar, no PHI fixtures."""
from app.week_people import (
    match_person,
    merge_client_hits,
    people_from_visits,
    person_from_visit,
)


def test_skips_busy_blocks():
    assert person_from_visit({"title": "Busy", "name": "", "email": ""}) is None
    assert person_from_visit({"title": "Lunch", "name": "", "email": ""}) is None


def test_uses_title_when_name_missing():
    p = person_from_visit({"title": "Alex Rivera", "name": "", "email": "a@example.com"})
    assert p is not None
    assert p["name"] == "Alex Rivera"
    assert p["source"] == "week"


def test_match_name_and_phone():
    p = {"name": "Alex Rivera", "email": "a@example.com", "phone": "+15551212"}
    assert match_person(p, "river")
    assert match_person(p, "a@example")
    assert match_person(p, "5551212")
    assert not match_person(p, "zzz")


def test_people_from_visits_dedupes():
    visits = [
        {"name": "Alex Rivera", "email": "a@example.com", "title": "Follow-up"},
        {"name": "Alex Rivera", "email": "a@example.com", "title": "Follow-up"},
        {"name": "", "email": "", "title": "Busy"},
        {"name": "Sam Lee", "email": "s@example.com", "title": "Intro"},
    ]
    rows = people_from_visits(visits, "alex", limit=12)
    emails = [r["email"] for r in rows]
    assert emails == ["a@example.com"]


def test_merge_prefers_directory():
    book = [{"id": "1", "name": "Alex Rivera", "email": "a@example.com", "phone": ""}]
    week = [
        {
            "id": "",
            "name": "Alex Rivera",
            "email": "a@example.com",
            "status": "this-week",
        },
        {"id": "", "name": "Sam Lee", "email": "s@example.com", "status": "this-week"},
    ]
    merged = merge_client_hits(book, week, limit=12)
    assert len(merged) == 2
    assert merged[0]["id"] == "1"
    assert merged[1]["email"] == "s@example.com"
