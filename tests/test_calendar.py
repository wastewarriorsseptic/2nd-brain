"""Run with: ./venv/bin/python tests/test_calendar.py

Exercises calendar_page() directly (bypassing HTTP - it only reads request.session) to check the
month-grid math (weekday alignment, week count, month wraparound) and that tasks are scoped
correctly by Universe and land on the right day, including completed ones.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A
from sqlmodel import create_engine, SQLModel, Session
from sqlalchemy.pool import StaticPool

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
A.engine = engine


class Req:
    def __init__(self, uid):
        self.session = {"user_id": uid}
        self.headers = {}


with Session(engine) as s:
    u = A.User(name="A", email="a@x.com")
    s.add(u)
    s.commit()
    s.refresh(u)
    uid = u.id

    home = A.Universe(name="Home", icon="🏠", kind="task", user_id=uid)
    people = A.Universe(name="People", icon="👥", kind="contact", user_id=uid)
    s.add(home)
    s.add(people)
    s.commit()
    s.refresh(home)
    s.refresh(people)

    r = A.Realm(name="R", user_id=uid, universe_id=home.id)
    s.add(r)
    s.commit()
    s.refresh(r)
    b = A.Bucket(name="B", realm_id=r.id)
    s.add(b)
    s.commit()
    s.refresh(b)

    # September 2026: one task on the 5th, one completed on the 20th, one just outside the month
    # (Aug 31 and Oct 1) that must NOT show up.
    s.add(A.Item(title="In-month pending", bucket_id=b.id, due_date=datetime(2026, 9, 5, 9, 0)))
    s.add(A.Item(title="In-month completed", bucket_id=b.id, due_date=datetime(2026, 9, 20, 14, 30), is_completed=True))
    s.add(A.Item(title="Before month", bucket_id=b.id, due_date=datetime(2026, 8, 31, 9, 0)))
    s.add(A.Item(title="After month", bucket_id=b.id, due_date=datetime(2026, 10, 1, 9, 0)))
    s.commit()
    home_id, people_id = home.id, people.id

resp = A.calendar_page(Req(uid), universe_id=None, year=2026, month=9)
ctx = resp.context

# September 2026: Sep 1 is a Tuesday -> 2 leading blanks (Sun, Mon), 30 days -> 5 weeks
assert ctx["month_label"] == "September 2026", ctx["month_label"]
weeks = ctx["weeks"]
assert len(weeks) == 5, f"expected 5 weeks, got {len(weeks)}"
assert weeks[0][0] is None and weeks[0][1] is None and weeks[0][2]["day"] == 1, "Sep 1 should land on Tuesday (index 2)"
assert all(cell is None or "day" in cell for week in weeks for cell in week)

day5 = next(c for w in weeks for c in w if c and c["day"] == 5)
assert [t["title"] for t in day5["tasks"]] == ["In-month pending"]
assert day5["tasks"][0]["due_time"] == ""  # 9:00am is the "no specific time" sentinel

day20 = next(c for w in weeks for c in w if c and c["day"] == 20)
assert [t["title"] for t in day20["tasks"]] == ["In-month completed"]
assert day20["tasks"][0]["is_completed"] is True
assert day20["tasks"][0]["due_time"] == "2:30 PM"

# nothing leaked in from adjacent months
all_titles = {t["title"] for w in weeks for c in w if c for t in c["tasks"]}
assert "Before month" not in all_titles and "After month" not in all_titles

# prev/next month wraparound at year boundaries
resp_dec = A.calendar_page(Req(uid), universe_id=None, year=2026, month=12)
assert (resp_dec.context["next_year"], resp_dec.context["next_month"]) == (2027, 1)
assert (resp_dec.context["prev_year"], resp_dec.context["prev_month"]) == (2026, 11)
resp_jan = A.calendar_page(Req(uid), universe_id=None, year=2027, month=1)
assert (resp_jan.context["prev_year"], resp_jan.context["prev_month"]) == (2026, 12)

# selecting the Contact-kind Universe yields an empty grid (no due-dated Items there)
resp_people = A.calendar_page(Req(uid), universe_id=people_id, year=2026, month=9)
assert all(not c["tasks"] for w in resp_people.context["weeks"] for c in w if c)

# selecting the Home Universe still finds its own tasks
resp_home = A.calendar_page(Req(uid), universe_id=home_id, year=2026, month=9)
day5b = next(c for w in resp_home.context["weeks"] for c in w if c and c["day"] == 5)
assert len(day5b["tasks"]) == 1

print("ALL TESTS PASSED")
