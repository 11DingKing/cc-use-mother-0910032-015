"""服务层：包比较与受影响未来预约分析。"""
from __future__ import annotations

from datetime import date
from typing import Any

from .checks import compare_graphs
from .store import Store


class CurriculumService:
    def __init__(self, store: Store):
        self.store = store

    def compare_packages(self, course_id_a: str, version_id_a: str,
                         course_id_b: str | None = None,
                         version_id_b: str | None = None) -> dict[str, Any]:
        """比较两个冻结课程包（方向：a -> b）。b 缺省为本课程当前版本。"""
        conn = self.store._connect()  # noqa: SLF001
        try:
            course = self.store.get_course(conn, course_id_a)
            target_course = course_id_b or course_id_a
            target_version = version_id_b or course["current_version_id"]
            if not target_version:
                from .errors import ConflictError
                raise ConflictError("目标课程尚无已发布版本")
            row_a = self.store.get_version(conn, course_id_a, version_id_a)
            row_b = self.store.get_version(conn, target_course, target_version)
            if row_a["status"] != "frozen" or row_b["status"] != "frozen":
                from .errors import ConflictError
                raise ConflictError("只能比较已冻结的课程包")
            graph_a = self.store.graph_of(row_a)
            graph_b = self.store.graph_of(row_b)
            diff = compare_graphs(graph_a, graph_b)
            diff["from"] = {"course_id": course_id_a, "version_id": version_id_a,
                            "version_seq": row_a["version_seq"],
                            "fingerprint": row_a["fingerprint"]}
            diff["to"] = {"course_id": target_course, "version_id": target_version,
                          "version_seq": row_b["version_seq"],
                          "fingerprint": row_b["fingerprint"]}
            return diff
        finally:
            if self.store._mem is None:  # noqa: SLF001
                conn.close()

    def affected_future_reservations(self, course_id: str,
                                     today: date | None = None) -> dict[str, Any]:
        """列出受当前最新版本影响的未来预约。

        预约创建时锁定当时的冻结版本；当课程已发布更新版本时，对每条未来预约
        比较其锁定版本与当前版本，标注不兼容原因及涉及的节点/材料。
        """
        conn = self.store._connect()  # noqa: SLF001
        try:
            course = self.store.get_course(conn, course_id)
            current_id = course["current_version_id"]
            if current_id is None:
                from .errors import ConflictError
                raise ConflictError("课程尚无已发布版本")
            current = self.store.get_version(conn, course_id, current_id)
            reservations = self.store.future_reservations(course_id, today)
            results: list[dict[str, Any]] = []
            for resv in reservations:
                if resv["version_id"] == current_id:
                    affected = False
                    diff: dict[str, Any] | None = None
                else:
                    pinned = self.store.get_version(conn, course_id, resv["version_id"])
                    if pinned["status"] != "frozen":
                        continue
                    diff = compare_graphs(self.store.graph_of(pinned),
                                          self.store.graph_of(current))
                    affected = diff["breaking"]
                results.append({
                    "reservation_id": resv["id"],
                    "school_contact": resv["school_contact"],
                    "scheduled_date": resv["scheduled_date"],
                    "party_size": resv["party_size"],
                    "pinned_version_id": resv["version_id"],
                    "current_version_id": current_id,
                    "affected": affected,
                    "reasons": diff["reasons"] if diff else [],
                    "affected_nodes": sorted(set(
                        (diff or {}).get("removed_nodes", [])
                        + (diff or {}).get("suspended_nodes", [])
                        + (diff or {}).get("replaced_nodes", [])
                    )) if diff else [],
                    "diff": diff,
                })
            flagged = [r for r in results if r["affected"]]
            return {
                "course_id": course_id,
                "current_version_id": current_id,
                "checked_on": (today or date.today()).isoformat(),
                "total_future": len(results),
                "affected_count": len(flagged),
                "reservations": results,
            }
        finally:
            if self.store._mem is None:  # noqa: SLF001
                conn.close()
