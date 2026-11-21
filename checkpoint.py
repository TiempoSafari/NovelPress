"""Local export checkpoints. Each completed resource is one SQLite transaction."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import requests


class Checkpoint:
    def __init__(self, root, url):
        self.path = Path(root) / (hashlib.sha256(url.strip().encode()).hexdigest() + ".sqlite")

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path, timeout=30)

    def start(self, snapshot):
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS resources(kind TEXT, key TEXT, value BLOB, PRIMARY KEY(kind,key))")
            db.execute("INSERT OR REPLACE INTO resources VALUES('task','snapshot',?)",
                       (json.dumps(snapshot, ensure_ascii=False),))

    def snapshot(self):
        if not self.path.exists():
            return None
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='resources'").fetchone():
                return None
        raw = self.get("task", "snapshot")
        return json.loads(raw) if raw else None

    def get(self, kind, key):
        with closing(self.connect()) as db:
            row = db.execute("SELECT value FROM resources WHERE kind=? AND key=?", (kind,key)).fetchone()
            return row[0] if row else None

    def put(self, kind, key, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO resources VALUES(?,?,?)", (kind,key,value))

    def completed(self):
        with closing(self.connect()) as db:
            return {r[0] for r in db.execute("SELECT key FROM resources WHERE kind='chapter'")}


def retry_network(action, check, notify=lambda attempt: None):
    for attempt in range(3):
        check()
        try:
            return action()
        except Exception as error:
            cause = error
            while cause.__cause__ is not None:
                cause = cause.__cause__
            transient = isinstance(cause, (requests.Timeout, requests.ConnectionError,
                                           requests.exceptions.ChunkedEncodingError))
            delay = 2 ** attempt
            if isinstance(cause, requests.HTTPError) and cause.response is not None:
                transient = cause.response.status_code in (408, 429, 500, 502, 503, 504)
                retry_after = cause.response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
                    transient = transient and delay <= 60
            if not transient or attempt == 2:
                raise
            notify(attempt + 1)
            until = time.monotonic() + delay
            while time.monotonic() < until:
                check()
                time.sleep(min(.1, max(0, until-time.monotonic())))
