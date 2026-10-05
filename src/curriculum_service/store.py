"""SQLite 持久化层：课程草稿、冻结课程包、版本链与预约。

版本链通过 ``parent_id`` 表达：新版本指向前一版本；跨课程复制产生的首版
指向被复制的源课程包。每条记录同时冗余 ``root_package_id``，便于沿整条
谱系查询受影响的未来预约。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS course_series (
    code         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    draft        TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    code             TEXT NOT NULL,
    version          INTEGER NOT NULL,
    parent_id        INTEGER REFERENCES packages(id),
    root_package_id  INTEGER NOT NULL,
    operation        TEXT NOT NULL,           -- publish | emergency_replace | suspend_nodes | copy
    reason           TEXT NOT NULL DEFAULT '',
    operator         TEXT NOT NULL DEFAULT '',
    fingerprint      TEXT NOT NULL,
    snapshot         TEXT NOT NULL,          -- 冻结的完整课程图谱
    suspended_nodes  TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    UNIQUE(code, version)
);

CREATE TABLE IF NOT EXISTS reservations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id    INTEGER NOT NULL REFERENCES packages(id),
    school        TEXT NOT NULL,
    contact       TEXT NOT NULL,
    grade_age     INTEGER,
    scheduled_at  TEXT NOT NULL,             -- ISO 日期时间
    status        TEXT NOT NULL DEFAULT 'booked',  -- booked | cancelled
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_code ON packages(code, version);
CREATE INDEX IF NOT EXISTS idx_packages_root ON packages(root_package_id);
CREATE INDEX IF NOT EXISTS idx_reservations_pkg ON reservations(package_id, scheduled_at);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """线程安全的 SQLite 封装。所有写操作在同一把锁内串行化。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 草稿

    def upsert_draft(self, course: dict[str, Any], fingerprint: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO course_series(code, title, draft, fingerprint, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                     title=excluded.title, draft=excluded.draft,
                     fingerprint=excluded.fingerprint, updated_at=excluded.updated_at""",
                (course["code"], course["title"], json.dumps(course, ensure_ascii=False),
                 fingerprint, now_iso()),
            )

    def get_draft(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT draft FROM course_series WHERE code = ?", (code,)
            ).fetchone()
        return json.loads(row["draft"]) if row else None

    def list_series(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.code, s.title, s.fingerprint, s.updated_at,
                          (SELECT MAX(version) FROM packages p WHERE p.code = s.code) AS published_version
                   FROM course_series s ORDER BY s.code"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 发布

    def transaction(self) -> "Transaction":
        """获取写事务（BEGIN IMMEDIATE），用于并发发布线性化。"""
        return Transaction(self)

    def latest_package(self, code: str, conn: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        conn = conn or self._conn
        return conn.execute(
            "SELECT * FROM packages WHERE code = ? ORDER BY version DESC LIMIT 1", (code,)
        ).fetchone()

    def get_package(self, package_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM packages WHERE id = ?", (package_id,)
            ).fetchone()

    def insert_package(
        self,
        *,
        code: str,
        version: int,
        parent_id: int | None,
        root_package_id: int,
        operation: str,
        reason: str,
        operator: str,
        fingerprint: str,
        snapshot: dict[str, Any],
        suspended_nodes: list[str],
        conn: sqlite3.Connection | None = None,
    ) -> int:
        conn = conn or self._conn
        cur = conn.execute(
            """INSERT INTO packages(code, version, parent_id, root_package_id, operation,
                                    reason, operator, fingerprint, snapshot,
                                    suspended_nodes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, version, parent_id, root_package_id, operation, reason, operator,
             fingerprint, json.dumps(snapshot, ensure_ascii=False),
             json.dumps(suspended_nodes, ensure_ascii=False), now_iso()),
        )
        return int(cur.lastrowid)

    def list_packages(self, code: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM packages WHERE code = ? ORDER BY version", (code,)
            ).fetchall())

    def chain_from(self, package_id: int) -> list[sqlite3.Row]:
        """沿 parent_id 自给定课程包向根回溯。"""
        chain: list[sqlite3.Row] = []
        with self._lock:
            current = self.get_package(package_id)
            seen: set[int] = set()
            while current is not None and current["id"] not in seen:
                chain.append(current)
                seen.add(current["id"])
                if current["parent_id"] is None:
                    break
                current = self.get_package(current["parent_id"])
        return chain

    def lineage_tip_ids(self, root_package_id: int) -> list[int]:
        """谱系内各课程编号的最新版本 id（用于预约影响范围）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT MAX(version) AS v, code FROM packages
                   WHERE root_package_id = ? GROUP BY code""",
                (root_package_id,),
            ).fetchall()
            ids = []
            for r in rows:
                row = self._conn.execute(
                    "SELECT id FROM packages WHERE code = ? AND version = ?",
                    (r["code"], r["v"]),
                ).fetchone()
                ids.append(row["id"])
        return ids

    def all_roots(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT root_package_id FROM packages"
            ).fetchall()
        return [r["root_package_id"] for r in rows]

    # ------------------------------------------------------------------ 预约

    def add_reservation(
        self,
        *,
        package_id: int,
        school: str,
        contact: str,
        grade_age: int | None,
        scheduled_at: str,
        note: str,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO reservations(package_id, school, contact, grade_age,
                                            scheduled_at, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (package_id, school, contact, grade_age, scheduled_at, note, now_iso()),
            )
            return int(cur.lastrowid)

    def cancel_reservation(self, reservation_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE reservations SET status = 'cancelled' WHERE id = ? AND status = 'booked'",
                (reservation_id,),
            )
            return cur.rowcount > 0

    def list_reservations(
        self, *, package_ids: list[int] | None = None, future_only: bool = False
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM reservations WHERE 1=1"
        params: list[Any] = []
        if package_ids is not None:
            if not package_ids:
                return []
            sql += f" AND package_id IN ({','.join('?' * len(package_ids))})"
            params.extend(package_ids)
        if future_only:
            sql += " AND scheduled_at >= ? AND status = 'booked'"
            params.append(now_iso())
        sql += " ORDER BY scheduled_at"
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())


class Transaction:
    """BEGIN IMMEDIATE 事务：同一时刻只有一个写事务持有锁。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._held = False

    def __enter__(self) -> sqlite3.Connection:
        self._store._lock.acquire()
        self._held = True
        conn = self._store._conn
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._held:
            return
        conn = self._store._conn
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            self._store._lock.release()
            self._held = False


def package_brief(row: sqlite3.Row) -> dict[str, Any]:
    """课程包元信息（不含完整快照）。"""
    return {
        "id": row["id"],
        "code": row["code"],
        "version": row["version"],
        "parent_id": row["parent_id"],
        "root_package_id": row["root_package_id"],
        "operation": row["operation"],
        "reason": row["reason"],
        "operator": row["operator"],
        "fingerprint": row["fingerprint"],
        "suspended_nodes": json.loads(row["suspended_nodes"]),
        "created_at": row["created_at"],
    }


def package_full(row: sqlite3.Row) -> dict[str, Any]:
    data = package_brief(row)
    data["snapshot"] = json.loads(row["snapshot"])
    return data


def reservation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "package_id": row["package_id"],
        "school": row["school"],
        "contact": row["contact"],
        "grade_age": row["grade_age"],
        "scheduled_at": row["scheduled_at"],
        "status": row["status"],
        "note": row["note"],
        "created_at": row["created_at"],
    }
