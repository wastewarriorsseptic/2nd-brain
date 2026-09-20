"""Run with: ./venv/bin/python tests/test_ai_favorite.py  - the AI chat's favorite_task tool."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A
from sqlmodel import create_engine, SQLModel, Session, select
from sqlalchemy.pool import StaticPool

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
A.engine = engine

with Session(engine) as s:
    u = A.User(name="A", email="a@x.com"); v = A.User(name="B", email="b@x.com")
    s.add(u); s.add(v); s.commit(); s.refresh(u); s.refresh(v)
    un = A.Universe(name="Home", kind="task", user_id=u.id); s.add(un); s.commit(); s.refresh(un)
    r = A.Realm(name="R", user_id=u.id, universe_id=un.id); s.add(r); s.commit(); s.refresh(r)
    b = A.Bucket(name="B", realm_id=r.id); s.add(b); s.commit(); s.refresh(b)
    it = A.Item(title="Dentist", bucket_id=b.id, due_date=datetime.now() + timedelta(days=1), description="d")
    s.add(it); s.commit(); s.refresh(it)
    uid, vid, iid = u.id, v.id, it.id

notes = lambda s: len(s.exec(select(A.Note).where(A.Note.source_item_id == iid)).all())

with Session(engine) as s:
    U, V = s.get(A.User, uid), s.get(A.User, vid)
    r1 = A._ai_execute_favorite_task(s, U, {"task_id": iid})
    assert r1["favorited"] and not r1["already_that_way"] and notes(s) == 1
    r2 = A._ai_execute_favorite_task(s, U, {"task_id": iid, "favorited": True})
    assert r2["already_that_way"] and notes(s) == 1
    r3 = A._ai_execute_favorite_task(s, U, {"task_id": iid, "favorited": False})
    assert not r3["favorited"] and not r3["already_that_way"] and notes(s) == 0
    assert "error" in A._ai_execute_favorite_task(s, V, {"task_id": iid}), "other users' tasks must be refused"

print("ALL TESTS PASSED")
