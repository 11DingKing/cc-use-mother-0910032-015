"""领域服务：课程版本链与发布工作流。

操作产生的版本链::

    publish            v(n) parent -> v(n-1)
    emergency_replace  v(n) parent -> v(n-1)，冻结时替换指定节点
    suspend_nodes      v(n) parent -> v(n-1)，快照中标记节点停用
    copy               新课程 v1 parent -> 源课程包（跨谱系指针）

发布在 ``BEGIN IMMEDIATE`` 事务内重新读取最新版本号并复核期望基线，
两个并发发布中第二个会收到 :class:`ServiceError`（版本冲突），不会静默覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from .errors import ServiceError
from .graph import content_fingerprint, normalize_course, validate_course
from .store import Store, package_brief, package_full, reservation_dict

WRITE_OPERATIONS = {"publish", "emergency_replace", "suspend_nodes", "copy"}


class CurriculumService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ 草稿

    def save_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        """内容负责人更新课程草稿（前置主题等）。不影响已发布课程包。"""
        try:
            course = normalize_course(payload)
        except ValueError as exc:
            raise ServiceError("invalid_course", str(exc), status=422) from exc
        fingerprint = content_fingerprint(course)
        self.store.upsert_draft(course, fingerprint)
        return {"code": course["code"], "title": course["title"], "fingerprint": fingerprint}

    def get_draft(self, code: str) -> dict[str, Any]:
        course = self.store.get_draft(code)
        if course is None:
            raise ServiceError("not_found", f"课程 {code} 不存在", status=404)
        return course

    def validate_draft(self, code: str, *, as_of: date | None = None) -> dict[str, Any]:
        course = self.get_draft(code)
        issues = validate_course(course, as_of=as_of)
        return {
            "code": code,
            "fingerprint": content_fingerprint(course),
            "ok": not issues,
            "issues": [i.to_dict() for i in issues],
        }

    # ------------------------------------------------------------------ 发布

    def publish(
        self,
        code: str,
        *,
        operator: str = "",
        expected_version: int | None = None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """发布前检测循环、缺失条件、年龄、材料与授权期限，通过后冻结完整图谱。"""
        course = self.get_draft(code)
        issues = validate_course(course, as_of=as_of)
        if issues:
            raise ServiceError(
                "validation_failed",
                f"课程 {code} 未通过发布前校验，共 {len(issues)} 个问题",
                status=422,
                issues=issues,
            )
        fingerprint = content_fingerprint(course)
        with self.store.transaction() as conn:
            latest = self.store.latest_package(code, conn)
            current_version = latest["version"] if latest else 0
            if expected_version is not None and expected_version != current_version:
                raise ServiceError(
                    "version_conflict",
                    f"期望基线版本 v{expected_version}，当前已发布 v{current_version}",
                    status=409,
                )
            # 内容未变不重复冻结，直接返回当前课程包，保证版本链语义明确
            if latest is not None and latest["fingerprint"] == fingerprint:
                pid = latest["id"]
                parent_id = latest["parent_id"]
                root_id = latest["root_package_id"]
                version = current_version
                created = False
            else:
                parent_id = latest["id"] if latest else None
                root_id = latest["root_package_id"] if latest else 0
                version = current_version + 1
                pid = self.store.insert_package(
                    conn=conn, code=code, version=version, parent_id=parent_id,
                    root_package_id=root_id, operation="publish", reason="",
                    operator=operator, fingerprint=fingerprint, snapshot=course,
                    suspended_nodes=[],
                )
                if root_id == 0:
                    conn.execute(
                        "UPDATE packages SET root_package_id = id WHERE id = ?", (pid,)
                    )
                created = True
        return self._publish_result(pid, version, created)

    def _publish_result(self, package_id: int, version: int, created: bool) -> dict[str, Any]:
        row = self.store.get_package(package_id)
        assert row is not None
        result = package_brief(row)
        result["created"] = created
        result["version_label"] = f"v{version}"
        return result

    # --------------------------------------------------------- 紧急替代/停用

    def emergency_replace(
        self,
        code: str,
        node_id: str,
        replacement: dict[str, Any],
        *,
        reason: str,
        operator: str = "",
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """紧急替代某个主题/环节：替换节点后重新校验并冻结为新版本。"""
        row = self._require_latest(code)
        course = json.loads(row["snapshot"])
        if not any(n["id"] == node_id for n in course["nodes"]):
            raise ServiceError("node_not_found", f"课程包中没有节点 {node_id}", status=404)
        try:
            replacement_norm = normalize_course(
                {"code": course["code"], "title": course["title"], "nodes": [replacement]}
            )["nodes"][0]
        except ValueError as exc:
            raise ServiceError("invalid_node", str(exc), status=422) from exc
        if replacement_norm["id"] != node_id:
            raise ServiceError(
                "invalid_node", "替代节点 id 必须与被替代节点一致", status=422
            )
        course["nodes"] = [
            replacement_norm if n["id"] == node_id else n for n in course["nodes"]
        ]
        issues = validate_course(course, as_of=as_of)
        if issues:
            raise ServiceError(
                "validation_failed", "紧急替代后的课程包未通过校验",
                status=422, issues=issues,
            )
        return self._append_version(
            course, operation="emergency_replace", reason=reason, operator=operator,
            suspended_nodes=[],
        )

    def suspend_nodes(
        self,
        code: str,
        node_ids: list[str],
        *,
        reason: str,
        operator: str = "",
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """局部停用节点：停用后复核依赖完整性（不得留下悬空前置）并冻结新版本。"""
        if not node_ids:
            raise ServiceError("invalid_request", "停用节点列表不能为空", status=400)
        row = self._require_latest(code)
        course = json.loads(row["snapshot"])
        existing = {n["id"] for n in course["nodes"]}
        unknown = [nid for nid in node_ids if nid not in existing]
        if unknown:
            raise ServiceError(
                "node_not_found", "节点不存在：" + "、".join(unknown), status=404
            )
        # 已停用的节点不再参与“停用后”的悬空检测
        already_suspended = set(json.loads(row["suspended_nodes"]))
        suspended = sorted(already_suspended | set(node_ids))
        active = [n for n in course["nodes"] if n["id"] not in suspended]
        active_ids = {n["id"] for n in active}
        issues = validate_course(
            {**course, "nodes": active}, as_of=as_of
        )
        # 停用允许剩余实践环节失去全部前置以外的结构性问题仍需拦截
        blocking = [i for i in issues if i.code not in {"missing_prerequisite"}]
        if blocking:
            raise ServiceError(
                "validation_failed", "局部停用会破坏课程包完整性",
                status=422, issues=blocking,
            )
        return self._append_version(
            course, operation="suspend_nodes", reason=reason, operator=operator,
            suspended_nodes=suspended,
        )

    # ------------------------------------------------------------------ 复制

    def copy_course(
        self,
        source_code: str,
        new_code: str,
        *,
        title: str | None = None,
        operator: str = "",
    ) -> dict[str, Any]:
        """复制已发布课程为新课程：v1 冻结源快照，parent 指向源课程包。"""
        if self.store.get_draft(new_code) is not None or self.store.latest_package(new_code):
            raise ServiceError("already_exists", f"目标课程 {new_code} 已存在", status=409)
        source_row = self._require_latest(source_code)
        snapshot = json.loads(source_row["snapshot"])
        snapshot = {**snapshot, "code": new_code, "title": title or snapshot["title"]}
        try:
            snapshot = normalize_course(snapshot)
        except ValueError as exc:
            raise ServiceError("invalid_course", str(exc), status=422) from exc
        fingerprint = content_fingerprint(snapshot)
        with self.store.transaction() as conn:
            source_latest = self.store.latest_package(source_code, conn)
            assert source_latest is not None
            if self.store.latest_package(new_code, conn) is not None:
                raise ServiceError(
                    "already_exists", f"目标课程 {new_code} 已存在", status=409
                )
            pid = self.store.insert_package(
                conn=conn, code=new_code, version=1, parent_id=source_latest["id"],
                root_package_id=source_latest["root_package_id"], operation="copy",
                reason=f"复制自 {source_code} v{source_latest['version']}",
                operator=operator, fingerprint=fingerprint, snapshot=snapshot,
                suspended_nodes=[],
            )
        self.store.upsert_draft(snapshot, fingerprint)
        row = self.store.get_package(pid)
        return package_brief(row)

    # ------------------------------------------------------------------ 查询

    def get_package(self, package_id: int) -> dict[str, Any]:
        row = self.store.get_package(package_id)
        if row is None:
            raise ServiceError("not_found", f"课程包 {package_id} 不存在", status=404)
        return package_full(row)

    def list_packages(self, code: str) -> list[dict[str, Any]]:
        self._require_latest(code)
        return [package_brief(r) for r in self.store.list_packages(code)]

    def version_chain(self, package_id: int) -> dict[str, Any]:
        rows = self.store.chain_from(package_id)
        if not rows:
            raise ServiceError("not_found", f"课程包 {package_id} 不存在", status=404)
        return {
            "package_id": package_id,
            "length": len(rows),
            "chain": [package_brief(r) for r in rows],
        }

    def compare_packages(self, package_id_a: int, package_id_b: int) -> dict[str, Any]:
        """比较两个课程包：版本链关系与节点/材料/年龄/授权/停用差异。"""
        row_a = self.store.get_package(package_id_a)
        row_b = self.store.get_package(package_id_b)
        if row_a is None or row_b is None:
            missing = package_id_a if row_a is None else package_id_b
            raise ServiceError("not_found", f"课程包 {missing} 不存在", status=404)
        snap_a = json.loads(row_a["snapshot"])
        snap_b = json.loads(row_b["snapshot"])
        nodes_a = {n["id"]: n for n in snap_a["nodes"]}
        nodes_b = {n["id"]: n for n in snap_b["nodes"]}
        suspended_a = set(json.loads(row_a["suspended_nodes"]))
        suspended_b = set(json.loads(row_b["suspended_nodes"]))

        added = sorted(set(nodes_b) - set(nodes_a))
        removed = sorted(set(nodes_a) - set(nodes_b))
        changed: list[dict[str, Any]] = []
        for nid in sorted(set(nodes_a) & set(nodes_b)):
            a, b = nodes_a[nid], nodes_b[nid]
            fields: dict[str, Any] = {}
            for field in ("type", "title", "age_min", "age_max",
                          "depends_on", "required_materials", "license"):
                if a.get(field) != b.get(field):
                    fields[field] = {"from": a.get(field), "to": b.get(field)}
            was_suspended = nid in suspended_a
            is_suspended = nid in suspended_b
            if was_suspended != is_suspended:
                fields["suspended"] = {"from": was_suspended, "to": is_suspended}
            if fields:
                changed.append({"node": nid, "fields": fields})

        chain_a = {r["id"] for r in self.store.chain_from(package_id_a)}
        relation = "unrelated"
        if package_id_a == package_id_b:
            relation = "same"
        elif package_id_b in chain_a:
            relation = "b_is_ancestor_of_a"
        else:
            chain_b = {r["id"] for r in self.store.chain_from(package_id_b)}
            if package_id_a in chain_b:
                relation = "a_is_ancestor_of_b"
            elif chain_a & chain_b:
                relation = "diverged_from_common_ancestor"

        return {
            "a": package_brief(row_a),
            "b": package_brief(row_b),
            "relation": relation,
            "compatible": relation in {"same", "a_is_ancestor_of_b"} and not added
            and not removed and not changed,
            "nodes_added": added,
            "nodes_removed": removed,
            "nodes_changed": changed,
            "materials": self._material_diff(snap_a, snap_b),
            "age_range": {
                "from": [snap_a["age_min"], snap_a["age_max"]],
                "to": [snap_b["age_min"], snap_b["age_max"]],
            },
            "fingerprint_changed": row_a["fingerprint"] != row_b["fingerprint"],
        }

    @staticmethod
    def _material_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
        ma = {m for n in a["nodes"] for m in n["required_materials"]}
        mb = {m for n in b["nodes"] for m in n["required_materials"]}
        return {
            "required_added": sorted(mb - ma),
            "required_removed": sorted(ma - mb),
        }

    # ------------------------------------------------------------------ 预约

    def create_reservation(self, payload: dict[str, Any]) -> dict[str, Any]:
        package_id = payload.get("package_id")
        if not isinstance(package_id, int):
            raise ServiceError("invalid_request", "package_id 必须是整数", status=400)
        if self.store.get_package(package_id) is None:
            raise ServiceError("not_found", f"课程包 {package_id} 不存在", status=404)
        school = str(payload.get("school") or "").strip()
        contact = str(payload.get("contact") or "").strip()
        scheduled_at = str(payload.get("scheduled_at") or "").strip()
        if not school or not contact or not scheduled_at:
            raise ServiceError(
                "invalid_request", "school/contact/scheduled_at 不能为空", status=400
            )
        grade_age = payload.get("grade_age")
        if grade_age is not None and not isinstance(grade_age, int):
            raise ServiceError("invalid_request", "grade_age 必须是整数", status=400)
        rid = self.store.add_reservation(
            package_id=package_id, school=school, contact=contact,
            grade_age=grade_age, scheduled_at=scheduled_at,
            note=str(payload.get("note") or ""),
        )
        row = self.store.list_reservations(package_ids=[rid])
        return reservation_dict(row[0]) if row else {"id": rid}

    def cancel_reservation(self, reservation_id: int) -> dict[str, Any]:
        if not self.store.cancel_reservation(reservation_id):
            raise ServiceError("not_found", "预约不存在或已取消", status=404)
        return {"id": reservation_id, "status": "cancelled"}

    def affected_future_reservations(self, package_id: int) -> dict[str, Any]:
        """列出因给定课程包所属谱系（复制产生的分支也包含）变化而受影响的未来预约。"""
        row = self.store.get_package(package_id)
        if row is None:
            raise ServiceError("not_found", f"课程包 {package_id} 不存在", status=404)
        tip_by_code = {
            self.store.get_package(tid)["code"]: tid
            for tid in self.store.lineage_tip_ids(row["root_package_id"])
        }
        affected: list[dict[str, Any]] = []
        for res in self.store.list_reservations(future_only=True):
            pkg = self.store.get_package(res["package_id"])
            if pkg is None or pkg["root_package_id"] != row["root_package_id"]:
                continue
            tip_id = tip_by_code.get(pkg["code"])
            if tip_id is not None and res["package_id"] != tip_id:
                data = reservation_dict(res)
                data["current_tip_package_id"] = tip_id
                data["reason"] = "预约绑定的课程包已被新版本取代"
                affected.append(data)
        return {
            "package_id": package_id,
            "root_package_id": row["root_package_id"],
            "count": len(affected),
            "reservations": affected,
        }

    # ------------------------------------------------------------------ 内部

    def _require_latest(self, code: str) -> sqlite3.Row:
        row = self.store.latest_package(code)
        if row is None:
            raise ServiceError("not_found", f"课程 {code} 尚无已发布课程包", status=404)
        return row

    def _append_version(
        self,
        course: dict[str, Any],
        *,
        operation: str,
        reason: str,
        operator: str,
        suspended_nodes: list[str],
    ) -> dict[str, Any]:
        fingerprint = content_fingerprint(course)
        with self.store.transaction() as conn:
            latest = self.store.latest_package(course["code"], conn)
            assert latest is not None
            version = latest["version"] + 1
            pid = self.store.insert_package(
                conn=conn, code=course["code"], version=version,
                parent_id=latest["id"], root_package_id=latest["root_package_id"],
                operation=operation, reason=reason, operator=operator,
                fingerprint=fingerprint, snapshot=course,
                suspended_nodes=suspended_nodes,
            )
        return self._publish_result(pid, version, True)
