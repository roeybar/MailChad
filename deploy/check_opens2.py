import sys; sys.path.insert(0, "/app")
from app import db; db.init()
with db.conn() as c:
    cursor = c.execute("SELECT value FROM sync_state WHERE key='last_pulled_event_id'").fetchone()
    print("sync cursor:", cursor["value"] if cursor else 0)
    inbox = c.execute("SELECT count(*) as n FROM inbox").fetchone()["n"]
    print("inbox rows:", inbox)
    whk = c.execute("SELECT count(*) as n FROM inbox WHERE table_name='webhook_event'").fetchone()["n"]
    print("webhook_event inbox:", whk)
    for j in c.execute("SELECT status, count(*) as n FROM dispatched_job GROUP BY status").fetchall():
        print("dispatched_job:", dict(j))
