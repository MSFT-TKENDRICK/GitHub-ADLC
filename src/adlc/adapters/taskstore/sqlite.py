"""SQLite task store -- the spine's credential-free default.

Used whenever no richer task-management system is registered/detected. Keeps the
graph, its dependency edges and per-node status in ``.adlc/tasks.db`` so a run
survives process restarts without needing GitHub.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from adlc.config import Config
from adlc.ports import TaskGraph

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    kind        TEXT,
    level       INTEGER,
    write_set   TEXT,
    acceptance  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    note        TEXT DEFAULT '',
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS task_deps (
    run_id     TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id, depends_on)
);
"""


class SqliteTaskStore:
    name = "sqlite"
    kind = "taskstore"

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path
        self._run_id: str | None = None

    @staticmethod
    def detect(cfg: Config) -> tuple[bool, str]:
        return True, "built-in SQLite store (always available)"

    def _connect(self, cfg: Config | None = None) -> sqlite3.Connection:
        path = self._db_path
        if path is None:
            root = cfg.adlc_dir if cfg else Path(".adlc")
            root.mkdir(parents=True, exist_ok=True)
            path = root / "tasks.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        return conn

    def bind(self, cfg: Config) -> None:
        """Attach this store to a config so `sync`/`update` know where to write."""
        self._db_path = cfg.adlc_dir / "tasks.db"

    def sync(self, graph: TaskGraph) -> dict[str, str]:
        run_id = graph.get("runId", "unknown")
        self._run_id = run_id
        mapping: dict[str, str] = {}
        with self._connect() as conn:
            for node in graph.get("nodes", []):
                node_id = node["id"]
                conn.execute(
                    "INSERT INTO tasks (run_id, node_id, title, kind, level, write_set, acceptance)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(run_id, node_id) DO UPDATE SET"
                    "   title=excluded.title, kind=excluded.kind, level=excluded.level,"
                    "   write_set=excluded.write_set, acceptance=excluded.acceptance",
                    (
                        run_id, node_id, node.get("title", ""), node.get("kind", "implement"),
                        node.get("level", 0), json.dumps(node.get("writeSet") or []),
                        json.dumps(node.get("acceptance") or []),
                    ),
                )
                conn.execute(
                    "DELETE FROM task_deps WHERE run_id = ? AND node_id = ?", (run_id, node_id)
                )
                for dep in node.get("dependsOn") or []:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_deps (run_id, node_id, depends_on)"
                        " VALUES (?, ?, ?)",
                        (run_id, node_id, dep),
                    )
                mapping[node_id] = f"sqlite:{run_id}/{node_id}"
        return mapping

    def update(self, node_id: str, status: str, note: str = "") -> None:
        with self._connect() as conn:
            # S608: every value is a bound parameter. The only concatenation is a
            # fixed literal selected by a boolean, so no caller input reaches SQL.
            conn.execute(
                "UPDATE tasks SET status = ?, note = ?, updated_at = CURRENT_TIMESTAMP"  # noqa: S608
                " WHERE node_id = ?" + (" AND run_id = ?" if self._run_id else ""),
                (status, note, node_id, *([self._run_id] if self._run_id else [])),
            )

    def ready(self, run_id: str) -> list[str]:
        """Nodes whose dependencies are all done -- useful for resume."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.node_id FROM tasks t WHERE t.run_id = ? AND t.status = 'pending'"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM task_deps d JOIN tasks dep"
                "     ON dep.run_id = d.run_id AND dep.node_id = d.depends_on"
                "   WHERE d.run_id = t.run_id AND d.node_id = t.node_id AND dep.status != 'done')",
                (run_id,),
            ).fetchall()
        return [row[0] for row in rows]
