import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
              created TEXT NOT NULL, latest_run TEXT);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request TEXT NOT NULL,
              status TEXT NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
              created TEXT NOT NULL, updated TEXT NOT NULL, parent_id TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_project ON runs(project_id)
              WHERE status IN ('running','stopping','waiting_confirmation','waiting_repair');
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              type TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run_id,id);
            CREATE TABLE IF NOT EXISTS decisions (
              run_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(run_id,id));
            CREATE TABLE IF NOT EXISTS services (
              id TEXT PRIMARY KEY, project_id TEXT, run_id TEXT, pid INTEGER,
              started REAL, payload TEXT);
        """)
        self.conn.execute("UPDATE runs SET status='interrupted' WHERE status IN ('running','stopping')")
        self.conn.commit()

    def project(self, project_id):
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError("项目不存在")
        return dict(row)

    def projects(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY created DESC")]

    def create_project(self, path: str, name: str):
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise ValueError("请选择已经存在的本地文件夹")
        if any(p.iterdir()):
            raise ValueError("新项目必须使用空文件夹，请另选目录；历史项目请从列表打开")
        project_id = uid()
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO projects VALUES(?,?,?,?,NULL)",
                              (project_id, str(p), name, now()))
        return self.project(project_id)

    def run(self, run_id):
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("执行不存在")
        data = dict(row)
        data["state"] = json.loads(data["state"])
        return data

    def runs(self, project_id):
        return [self.run(r[0]) for r in self.conn.execute(
            "SELECT id FROM runs WHERE project_id=? ORDER BY created DESC", (project_id,))]

    def create_run(self, project_id, request, state, parent_id=None):
        run_id = uid()
        stamp = now()
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                              (run_id, project_id, request, "running", "planning", json.dumps(state), stamp, stamp, parent_id))
            self.conn.execute("UPDATE projects SET latest_run=? WHERE id=?", (run_id, project_id))
        return self.run(run_id)

    def update(self, run_id, *, status=None, phase=None, state=None):
        with self.lock, self.conn:
            old = self.run(run_id)
            self.conn.execute("UPDATE runs SET status=?,phase=?,state=?,updated=? WHERE id=?",
                              (status or old["status"], phase or old["phase"],
                               json.dumps(state if state is not None else old["state"], ensure_ascii=False), now(), run_id))

    def event(self, run_id, event_type, payload):
        with self.lock, self.conn:
            cursor = self.conn.execute("INSERT INTO events(run_id,type,payload,created) VALUES(?,?,?,?)",
                                       (run_id, event_type, json.dumps(payload, ensure_ascii=False), now()))
            return cursor.lastrowid

    def events(self, run_id, after=0):
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.conn.execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 200", (run_id, after))]

    def decision(self, run_id, decision_id, payload=None):
        existing = self.conn.execute("SELECT payload FROM decisions WHERE run_id=? AND id=?",
                                     (run_id, decision_id)).fetchone()
        if payload is None:
            return json.loads(existing[0]) if existing else None
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO decisions VALUES(?,?,?)", (run_id, decision_id, json.dumps(payload)))

    def close(self):
        self.conn.close()
