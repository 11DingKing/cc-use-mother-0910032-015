"""SQLite 存储层：课程、版本链、冻结图谱、预约。

写操作使用 BEGIN IMMEDIATE，配合期望版本号（乐观锁）实现并发发布冲突检测。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .checks import effective_age, graph_fingerprint, validate_graph

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id),
    version_seq INTEGER,
    parent_version_id TEXT,
    base_version_id TEXT,
    copy_from_version_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT,
    change_summary TEXT,
    created_by TEXT,
    graph_json TEXT,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE(course_id, version_seq)
);
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id),
    version_id TEXT NOT NULL REFERENCES versions(id),
    school_contact TEXT NOT NULL,
    scheduled_date TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL,
    version_id TEXT,
    kind TEXT NOT NULL,
    actor TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""

VERSION_KINDS = {"initial", "revision", "emergency_replace", "suspension", "copy", "resume"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.Lock()
        if self.path == ":memory:":
            self._mem = sqlite3.connect(":memory:", check_same_thread=False,
                                        isolation_level=None)
            self._mem.row_factory = sqlite3.Row
            self._mem.executescript(SCHEMA)
        else:
            self._mem = None
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        if self._mem is not None:
            return self._mem
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _tx(self):
        """开启立即写事务（文件库）或普通事务（内存共享连接）。"""
        conn = self._connect()
        if self._mem is not None:
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    # ---- 基础工具 ----

    def _event(self, conn: sqlite3.Connection, course_id: str, kind: str,
               version_id: str | None, actor: str | None, detail: dict[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO events(course_id, version_id, kind, actor, detail, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (course_id, version_id, kind, actor,
             json.dumps(detail, ensure_ascii=False) if detail else None, now_iso()),
        )

    def get_course(self, conn: sqlite3.Connection, course_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if row is None:
            from .errors import NotFoundError
            raise NotFoundError(f"课程不存在：{course_id}")
        return row

    def get_version(self, conn: sqlite3.Connection, course_id: str, version_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM versions WHERE id=? AND course_id=?", (version_id, course_id)
        ).fetchone()
        if row is None:
            from .errors import NotFoundError
            raise NotFoundError(f"版本不存在：{version_id}")
        return row

    @staticmethod
    def graph_of(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["graph_json"])

    @staticmethod
    def version_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "course_id": row["course_id"],
            "version_seq": row["version_seq"],
            "kind": row["kind"],
            "status": row["status"],
            "parent_version_id": row["parent_version_id"],
            "base_version_id": row["base_version_id"],
            "copy_from_version_id": row["copy_from_version_id"],
            "fingerprint": row["fingerprint"],
            "change_summary": row["change_summary"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "frozen_at": row["frozen_at"],
        }

    # ---- 课程与草稿 ----

    def create_course(self, code: str, title: str, graph: dict[str, Any],
                      actor: str | None = None) -> dict[str, Any]:
        cid = uuid.uuid4().hex[:12]
        vid = uuid.uuid4().hex[:12]
        with self._lock, self._tx() as conn:
            if conn.execute("SELECT 1 FROM courses WHERE code=?", (code,)).fetchone():
                from .errors import ConflictError
                raise ConflictError(f"课程编码已存在：{code}")
            ts = now_iso()
            conn.execute(
                "INSERT INTO courses(id, code, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (cid, code, title, ts, ts),
            )
            conn.execute(
                "INSERT INTO versions(id, course_id, kind, status, graph_json,"
                " change_summary, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (vid, cid, "initial", "draft", json.dumps(graph, ensure_ascii=False),
                 "课程创建草稿", actor, ts),
            )
            self._event(conn, cid, "draft_created", vid, actor, {"kind": "initial"})
            return {"course_id": cid, "draft_version_id": vid}

    def replace_draft_graph(self, course_id: str, version_id: str, graph: dict[str, Any]) -> None:
        with self._lock, self._tx() as conn:
            self.get_course(conn, course_id)
            row = self.get_version(conn, course_id, version_id)
            if row["status"] != "draft":
                from .errors import ConflictError
                raise ConflictError("仅草稿状态可以修改图谱")
            conn.execute(
                "UPDATE versions SET graph_json=? WHERE id=?",
                (json.dumps(graph, ensure_ascii=False), version_id),
            )

    def _new_draft_from(self, conn: sqlite3.Connection, course_id: str, kind: str,
                        base_row: sqlite3.Row, summary: str, actor: str | None,
                        graph: dict[str, Any] | None = None) -> str:
        vid = uuid.uuid4().hex[:12]
        graph_json = base_row["graph_json"] if graph is None else json.dumps(graph, ensure_ascii=False)
        conn.execute(
            "INSERT INTO versions(id, course_id, kind, status, graph_json,"
            " base_version_id, copy_from_version_id, change_summary, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vid, course_id, kind, "draft", graph_json,
             base_row["id"], base_row["copy_from_version_id"], summary, actor, now_iso()),
        )
        self._event(conn, course_id, "draft_created", vid, actor,
                    {"kind": kind, "base_version_id": base_row["id"]})
        return vid

    def create_revision(self, course_id: str, actor: str | None = None) -> str:
        """基于当前已发布版本创建修订草稿。"""
        with self._lock, self._tx() as conn:
            course = self.get_course(conn, course_id)
            if not course["current_version_id"]:
                from .errors import ConflictError
                raise ConflictError("课程尚无已发布版本，无法创建修订")
            base = self.get_version(conn, course_id, course["current_version_id"])
            return self._new_draft_from(conn, course_id, "revision", base,
                                        "常规修订草稿", actor)

    def copy_course(self, course_id: str, new_code: str, new_title: str,
                    actor: str | None = None) -> dict[str, str]:
        """复制当前已发布课程包为一门新课程，副本草稿是独立版本链的起点。"""
        with self._lock, self._tx() as conn:
            src = self.get_course(conn, course_id)
            if not src["current_version_id"]:
                from .errors import ConflictError
                raise ConflictError("只能复制已发布的课程包")
            base = self.get_version(conn, course_id, src["current_version_id"])
            if conn.execute("SELECT 1 FROM courses WHERE code=?", (new_code,)).fetchone():
                from .errors import ConflictError
                raise ConflictError(f"课程编码已存在：{new_code}")
            cid = uuid.uuid4().hex[:12]
            ts = now_iso()
            conn.execute(
                "INSERT INTO courses(id, code, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (cid, new_code, new_title, ts, ts),
            )
            vid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO versions(id, course_id, kind, status, graph_json,"
                " copy_from_version_id, change_summary, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (vid, cid, "copy", "draft", base["graph_json"], base["id"],
                 f"复制自 {src['code']} {base['id']}", actor, ts),
            )
            self._event(conn, cid, "draft_created", vid, actor,
                        {"kind": "copy", "copy_from_version_id": base["id"]})
            return {"course_id": cid, "draft_version_id": vid,
                    "copy_from_version_id": base["id"]}

    def emergency_replace(self, course_id: str, node_id: str, replacement: dict[str, Any],
                          actor: str | None = None) -> str:
        """紧急替代：旧节点标记 replaced 并保留在图谱中，生效引用切换到替代节点。"""
        with self._lock, self._tx() as conn:
            course = self.get_course(conn, course_id)
            if not course["current_version_id"]:
                from .errors import ConflictError
                raise ConflictError("课程尚无已发布版本")
            base = self.get_version(conn, course_id, course["current_version_id"])
            graph = self.graph_of(base)
            nodes = graph["nodes"]
            target = next((n for n in nodes if n["id"] == node_id), None)
            if target is None:
                from .errors import NotFoundError
                raise NotFoundError(f"节点不存在：{node_id}")
            if target.get("status", "active") != "active":
                from .errors import ConflictError
                raise ConflictError(f"节点 {node_id} 非生效状态，无法替代")
            new_id = replacement.get("id") or f"repl-{uuid.uuid4().hex[:8]}"
            if any(n["id"] == new_id for n in nodes):
                from .errors import ConflictError
                raise ConflictError(f"替代节点编号已存在：{new_id}")
            target["status"] = "replaced"
            target["replaced_by"] = new_id
            new_node = {
                "id": new_id,
                "type": target["type"],
                "title": replacement["title"],
                "age_min": int(replacement.get("age_min", target["age_min"])),
                "age_max": int(replacement.get("age_max", target["age_max"])),
                "materials": replacement.get("materials", target.get("materials", [])),
                "authorization_valid_until": replacement["authorization_valid_until"],
                "status": "active",
                "replaces": node_id,
            }
            nodes.append(new_node)
            # 旧节点的全部依赖关系（入边与出边）改挂到替代节点，并去重
            relocated: list[dict[str, str]] = []
            for edge in graph["edges"]:
                if edge["source"] == node_id:
                    edge["source"] = new_id
                if edge["target"] == node_id:
                    edge["target"] = new_id
                relocated.append(edge)
            unique: set[tuple[str, str]] = set()
            deduped: list[dict[str, str]] = []
            for edge in relocated:
                pair = (edge["source"], edge["target"])
                if pair not in unique:
                    unique.add(pair)
                    deduped.append(edge)
            graph["edges"] = deduped
            summary = f"紧急替代：{node_id} -> {new_id}（{new_node['title']}）"
            return self._new_draft_from(conn, course_id, "emergency_replace", base,
                                        summary, actor, graph)

    def suspend_node(self, course_id: str, node_id: str, reason: str,
                     actor: str | None = None) -> str:
        """局部停用：把当前发布包中的单个节点标记为 suspended，产生停用草稿。"""
        with self._lock, self._tx() as conn:
            course = self.get_course(conn, course_id)
            if not course["current_version_id"]:
                from .errors import ConflictError
                raise ConflictError("课程尚无已发布版本")
            base = self.get_version(conn, course_id, course["current_version_id"])
            graph = self.graph_of(base)
            target = next((n for n in graph["nodes"] if n["id"] == node_id), None)
            if target is None:
                from .errors import NotFoundError
                raise NotFoundError(f"节点不存在：{node_id}")
            if target.get("status", "active") != "active":
                from .errors import ConflictError
                raise ConflictError(f"节点 {node_id} 已非生效状态")
            target["status"] = "suspended"
            target["suspended_reason"] = reason
            summary = f"局部停用：{node_id}（{target.get('title', '')}）原因：{reason}"
            return self._new_draft_from(conn, course_id, "suspension", base,
                                        summary, actor, graph)

    # ---- 校验与发布 ----

    def validate_draft(self, course_id: str, version_id: str,
                       today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        conn = self._connect()
        try:
            self.get_course(conn, course_id)
            row = self.get_version(conn, course_id, version_id)
            graph = self.graph_of(row)
            issues = validate_graph(graph, today)
            return {"ok": not issues, "issues": issues,
                    "fingerprint": graph_fingerprint(graph)}
        finally:
            if self._mem is None:
                conn.close()

    def publish_draft(self, course_id: str, version_id: str,
                      expected_version_id: str | None = None,
                      actor: str | None = None, today: date | None = None) -> dict[str, Any]:
        """冻结完整图谱。expected_version_id 为调用方认知的当前版本，实现并发冲突检测。"""
        from .errors import ConflictError, GraphValidationError
        today = today or date.today()
        with self._lock, self._tx() as conn:
            course = self.get_course(conn, course_id)
            row = self.get_version(conn, course_id, version_id)
            if row["status"] != "draft":
                raise ConflictError(f"版本 {version_id} 状态为 {row['status']}，不能重复发布")
            current_id = course["current_version_id"]
            # 并发发布：草稿基于的版本必须仍是课程当前版本
            base_id = row["base_version_id"]
            must_equal = expected_version_id or base_id
            if current_id is not None and must_equal is not None and current_id != must_equal:
                raise ConflictError(
                    "课程包已被其他发布推进，请基于最新版本重建草稿",
                    {"current_version_id": current_id,
                     "expected_version_id": must_equal},
                )

            graph = self.graph_of(row)
            issues = validate_graph(graph, today)
            if issues:
                raise GraphValidationError("图谱校验未通过，禁止发布", issues)

            fingerprint = graph_fingerprint(graph)
            dup = conn.execute(
                "SELECT id FROM versions WHERE course_id=? AND fingerprint=? AND status='frozen'",
                (course_id, fingerprint),
            ).fetchone()
            if dup is not None:
                raise ConflictError("图谱与已发布版本完全一致，无需重复发布",
                                    {"identical_version_id": dup["id"]})

            seq_row = conn.execute(
                "SELECT COALESCE(MAX(version_seq), 0) + 1 AS next FROM versions WHERE course_id=?",
                (course_id,),
            ).fetchone()
            seq = seq_row["next"]
            age_min, age_max = effective_age(graph)
            ts = now_iso()
            conn.execute(
                "UPDATE versions SET status='frozen', version_seq=?, parent_version_id=?,"
                " fingerprint=?, frozen_at=? WHERE id=?",
                (seq, current_id, fingerprint, ts, version_id),
            )
            conn.execute(
                "UPDATE courses SET current_version_id=?, updated_at=? WHERE id=?",
                (version_id, ts, course_id),
            )
            self._event(conn, course_id, "published", version_id, actor,
                        {"version_seq": seq, "fingerprint": fingerprint,
                         "parent_version_id": current_id, "age_range": [age_min, age_max]})
            return {"version_id": version_id, "version_seq": seq,
                    "parent_version_id": current_id, "fingerprint": fingerprint,
                    "frozen_at": ts, "age_range": [age_min, age_max]}

    # ---- 查询 ----

    def list_versions(self, course_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            self.get_course(conn, course_id)
            rows = conn.execute(
                "SELECT * FROM versions WHERE course_id=?"
                " ORDER BY COALESCE(version_seq, 999999), created_at, id",
                (course_id,),
            ).fetchall()
            return [self.version_dict(r) for r in rows]
        finally:
            if self._mem is None:
                conn.close()

    def get_manifest(self, course_id: str, version_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = self.get_version(conn, course_id, version_id)
            if row["status"] != "frozen":
                from .errors import ConflictError
                raise ConflictError("仅已冻结版本可以作为课程包读取")
            graph = self.graph_of(row)
            age_min, age_max = effective_age(graph)
            return {
                "version": self.version_dict(row),
                "age_range": [age_min, age_max],
                "node_count": len(graph["nodes"]),
                "edge_count": len(graph["edges"]),
                "graph": graph,
            }
        finally:
            if self._mem is None:
                conn.close()

    def list_courses(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM versions v WHERE v.course_id=c.id"
                " AND v.status='frozen') AS frozen_count FROM courses c ORDER BY c.created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            if self._mem is None:
                conn.close()

    def list_events(self, course_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            self.get_course(conn, course_id)
            rows = conn.execute(
                "SELECT * FROM events WHERE course_id=? ORDER BY id", (course_id,)
            ).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["detail"] = json.loads(item["detail"]) if item["detail"] else None
                out.append(item)
            return out
        finally:
            if self._mem is None:
                conn.close()

    # ---- 预约 ----

    def create_reservation(self, course_id: str, version_id: str | None,
                           school_contact: str, scheduled_date: str,
                           party_size: int) -> dict[str, Any]:
        from .errors import BadRequestError
        try:
            y, m, d = scheduled_date.split("-")
            sched = date(int(y), int(m), int(d))
        except (ValueError, AttributeError):
            raise BadRequestError("scheduled_date 必须是 YYYY-MM-DD")
        with self._lock, self._tx() as conn:
            self.get_course(conn, course_id)
            target_id = version_id
            if target_id is None:
                course = self.get_course(conn, course_id)
                target_id = course["current_version_id"]
                if target_id is None:
                    from .errors import ConflictError
                    raise ConflictError("课程尚无已发布版本，不能预约")
            row = self.get_version(conn, course_id, target_id)
            if row["status"] != "frozen":
                from .errors import ConflictError
                raise ConflictError("只能预约已冻结的课程包版本")
            rid = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO reservations(id, course_id, version_id, school_contact,"
                " scheduled_date, party_size, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rid, course_id, target_id, school_contact, sched.isoformat(),
                 party_size, "confirmed", now_iso()),
            )
            return {"reservation_id": rid, "course_id": course_id,
                    "version_id": target_id, "scheduled_date": sched.isoformat()}

    def future_reservations(self, course_id: str | None = None,
                            today_: date | None = None) -> list[dict[str, Any]]:
        today_ = today_ or date.today()
        conn = self._connect()
        try:
            sql = ("SELECT r.*, c.code AS course_code FROM reservations r"
                   " JOIN courses c ON c.id = r.course_id"
                   " WHERE r.status='confirmed' AND r.scheduled_date >= ?")
            params: list[Any] = [today_.isoformat()]
            if course_id:
                sql += " AND r.course_id = ?"
                params.append(course_id)
            sql += " ORDER BY r.scheduled_date, r.id"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            if self._mem is None:
                conn.close()
