"""领域服务与图谱校验测试。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from curriculum_service import CurriculumService, ServiceError, Store
from curriculum_service.graph import find_cycle, normalize_course, validate_course


def make_node(node_id, **over):
    node = {
        "id": node_id,
        "type": "topic",
        "title": f"节点{node_id}",
        "age_min": 7,
        "age_max": 12,
        "depends_on": [],
        "required_materials": [],
        "license": {
            "licensed_to": "市科技馆",
            "valid_from": "2026-01-01",
            "valid_until": "2028-01-01",
        },
    }
    node.update(over)
    return node


def make_course(nodes=None, **over):
    course = {
        "code": "SC-001",
        "title": "生态研学课",
        "age_min": 7,
        "age_max": 12,
        "available_materials": ["安全帽", "标本盒"],
        "nodes": nodes if nodes is not None else [
            make_node("T1"),
            make_node("P1", type="practice", depends_on=["T1"],
                      required_materials=["安全帽"]),
        ],
    }
    course.update(over)
    return course


class GraphValidationTest(unittest.TestCase):
    def issues(self, course, as_of="2026-10-05"):
        return validate_course(normalize_course(course), as_of=date.fromisoformat(as_of))

    def test_clean_course_has_no_issues(self):
        self.assertEqual(self.issues(make_course()), [])

    def test_cycle_is_detected(self):
        course = make_course(nodes=[
            make_node("T1", depends_on=["T3"]),
            make_node("T2", depends_on=["T1"]),
            make_node("T3", depends_on=["T2"]),
        ])
        issues = self.issues(course)
        codes = {i.code for i in issues}
        self.assertIn("cycle", codes)
        cycle = next(i for i in issues if i.code == "cycle").detail["cycle"]
        self.assertEqual(cycle[0], cycle[-1])

    def test_find_cycle_helper(self):
        nodes = normalize_course(make_course(nodes=[
            make_node("A", depends_on=["B"]),
            make_node("B", depends_on=["A"]),
        ]))["nodes"]
        self.assertIsNotNone(find_cycle(nodes))

    def test_missing_dependency_and_prerequisite(self):
        course = make_course(nodes=[
            make_node("T1", depends_on=["GONE"]),
            make_node("P1", type="practice"),
        ])
        codes = {i.code for i in self.issues(course)}
        self.assertIn("missing_dependency", codes)
        self.assertIn("missing_prerequisite", codes)

    def test_age_conflict(self):
        course = make_course(nodes=[make_node("T1", age_min=15, age_max=18)])
        codes = {i.code for i in self.issues(course)}
        self.assertIn("age_conflict", codes)

    def test_missing_material(self):
        course = make_course(nodes=[
            make_node("T1", required_materials=["显微镜"])
        ])
        issues = self.issues(course)
        self.assertEqual([i.code for i in issues], ["missing_material"])
        self.assertEqual(issues[0].detail["materials"], ["显微镜"])

    def test_license_expired_and_missing(self):
        course = make_course(nodes=[
            make_node("T1", license={"licensed_to": "X", "valid_from": "2025-01-01",
                                     "valid_until": "2026-09-01"}),
            make_node("T2", license={}),
        ])
        codes = {i.code for i in self.issues(course)}
        self.assertIn("license_expired", codes)
        self.assertIn("missing_license", codes)

    def test_license_must_cover_offering_window(self):
        # 发布时点有效，但开课窗口末端已超出授权期限
        course = make_course(
            offering_until="2029-06-01",
            nodes=[make_node("T1", license={"licensed_to": "X",
                                            "valid_from": "2026-01-01",
                                            "valid_until": "2027-01-01"})],
        )
        codes = {i.code for i in self.issues(course)}
        self.assertIn("license_expired", codes)


class ServiceWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.svc = CurriculumService(Store(":memory:"))
        self.svc.save_draft(make_course())

    def test_publish_freezes_snapshot_and_draft_change_is_isolated(self):
        v1 = self.svc.publish("SC-001", operator="统筹员甲")
        self.assertEqual(v1["version"], 1)
        self.assertTrue(v1["created"])

        # 内容负责人更新前置主题：草稿变化不影响已冻结的课程包
        self.svc.save_draft(make_course(nodes=[
            make_node("T1", title="更新后的主题"),
            make_node("P1", type="practice", depends_on=["T1"]),
        ]))
        frozen = self.svc.get_package(v1["id"])
        self.assertEqual(frozen["snapshot"]["nodes"][0]["title"], "节点T1")

        v2 = self.svc.publish("SC-001")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["parent_id"], v1["id"])
        self.assertEqual(v2["root_package_id"], v1["id"])

    def test_publish_rejects_invalid_course_and_reports_issues(self):
        self.svc.save_draft(make_course(nodes=[
            make_node("T1", depends_on=["T2"]),
            make_node("T2", depends_on=["T1"]),
        ]))
        with self.assertRaises(ServiceError) as ctx:
            self.svc.publish("SC-001")
        self.assertEqual(ctx.exception.code, "validation_failed")
        self.assertTrue(any(i.code == "cycle" for i in ctx.exception.issues))

    def test_republish_same_content_is_idempotent(self):
        v1 = self.svc.publish("SC-001")
        again = self.svc.publish("SC-001")
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], v1["id"])

    def test_emergency_replace_creates_version_and_revalidates(self):
        v1 = self.svc.publish("SC-001")
        replacement = make_node("T1", title="紧急替代主题",
                                license={"licensed_to": "市科技馆",
                                         "valid_from": "2026-01-01",
                                         "valid_until": "2028-01-01"})
        v2 = self.svc.emergency_replace("SC-001", "T1", replacement,
                                        reason="原讲解员请假", operator="场馆管理员")
        self.assertEqual(v2["operation"], "emergency_replace")
        self.assertEqual(v2["parent_id"], v1["id"])
        self.assertEqual(self.svc.get_package(v2["id"])["snapshot"]["nodes"][0]["title"],
                         "紧急替代主题")

        # 替代节点授权过期：拒绝冻结
        bad = make_node("T1", license={"licensed_to": "X",
                                       "valid_from": "2020-01-01",
                                       "valid_until": "2021-01-01"})
        with self.assertRaises(ServiceError) as ctx:
            self.svc.emergency_replace("SC-001", "T1", bad, reason="再替代")
        self.assertEqual(ctx.exception.code, "validation_failed")
        self.assertTrue(any(i.code == "license_expired" for i in ctx.exception.issues))

    def test_suspend_nodes_blocks_dangling_dependency(self):
        self.svc.publish("SC-001")
        # T1 被 P1 依赖，停用 T1 会留下悬空前置
        with self.assertRaises(ServiceError) as ctx:
            self.svc.suspend_nodes("SC-001", ["T1"], reason="设备故障")
        self.assertTrue(any(i.code == "missing_dependency" for i in ctx.exception.issues))

        v2 = self.svc.suspend_nodes("SC-001", ["P1"], reason="实践场地维护")
        self.assertEqual(v2["operation"], "suspend_nodes")
        self.assertEqual(v2["suspended_nodes"], ["P1"])
        chain = self.svc.version_chain(v2["id"])
        self.assertEqual([p["version"] for p in chain["chain"]], [2, 1])

    def test_copy_course_links_lineage(self):
        v1 = self.svc.publish("SC-001")
        copied = self.svc.copy_course("SC-001", "SC-002", title="分校版")
        self.assertEqual(copied["version"], 1)
        self.assertEqual(copied["parent_id"], v1["id"])
        self.assertEqual(copied["root_package_id"], v1["root_package_id"])
        self.assertEqual(copied["operation"], "copy")
        self.assertEqual(self.svc.get_package(copied["id"])["snapshot"]["code"], "SC-002")

    def test_compare_packages(self):
        v1 = self.svc.publish("SC-001")
        self.svc.save_draft(make_course(nodes=[
            make_node("T1", required_materials=["安全帽"], title="节点T1"),
            make_node("P1", type="practice", depends_on=["T1"],
                      required_materials=["安全帽", "标本盒"]),
        ]))
        v2 = self.svc.publish("SC-001")
        diff = self.svc.compare_packages(v1["id"], v2["id"])
        self.assertEqual(diff["relation"], "a_is_ancestor_of_b")
        changed = {c["node"]: c["fields"] for c in diff["nodes_changed"]}
        self.assertIn("P1", changed)
        self.assertEqual(changed["P1"]["required_materials"]["to"], ["安全帽", "标本盒"])
        self.assertIn("标本盒", diff["materials"]["required_added"])

        same = self.svc.compare_packages(v1["id"], v1["id"])
        self.assertEqual(same["relation"], "same")
        self.assertTrue(same["compatible"])

    def test_affected_future_reservations_across_lineage(self):
        v1 = self.svc.publish("SC-001")
        r_future = self.svc.create_reservation({
            "package_id": v1["id"], "school": "第一小学", "contact": "王老师",
            "grade_age": 9, "scheduled_at": "2030-05-01T09:00:00",
        })
        r_past = self.svc.create_reservation({
            "package_id": v1["id"], "school": "第二小学", "contact": "李老师",
            "scheduled_at": "2020-01-01T09:00:00",
        })
        r_cancelled = self.svc.create_reservation({
            "package_id": v1["id"], "school": "第三小学", "contact": "赵老师",
            "scheduled_at": "2030-06-01T09:00:00",
        })
        self.svc.cancel_reservation(r_cancelled["id"])

        # 内容变化后发布 v2
        self.svc.save_draft(make_course(nodes=[
            make_node("T1", title="修订主题"),
            make_node("P1", type="practice", depends_on=["T1"]),
        ]))
        v2 = self.svc.publish("SC-001")

        affected = self.svc.affected_future_reservations(v2["id"])
        ids = {r["id"] for r in affected["reservations"]}
        self.assertEqual(ids, {r_future["id"]})
        self.assertEqual(affected["reservations"][0]["current_tip_package_id"], v2["id"])

        # 复制分支上的预约同样受谱系影响
        copied = self.svc.copy_course("SC-001", "SC-009")
        r_copy = self.svc.create_reservation({
            "package_id": copied["id"], "school": "分校", "contact": "钱老师",
            "scheduled_at": "2030-07-01T09:00:00",
        })
        self.svc.save_draft({**self.svc.get_draft("SC-009"),
                             "title": "分校修订版"})
        copy_v2 = self.svc.publish("SC-009")
        affected2 = self.svc.affected_future_reservations(copy_v2["id"])
        self.assertIn(r_copy["id"], {r["id"] for r in affected2["reservations"]})

    def test_concurrent_publish_with_expected_version_conflicts(self):
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def worker():
            barrier.wait()
            try:
                outcomes.append(self.svc.publish("SC-001", expected_version=0))
            except ServiceError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        created = [o for o in outcomes if not isinstance(o, ServiceError)]
        conflicts = [o for o in outcomes if isinstance(o, ServiceError)]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].code, "version_conflict")
        self.assertEqual(conflicts[0].status, 409)

    def test_concurrent_publish_without_baseline_linearizes(self):
        barrier = threading.Barrier(2)
        outcomes: list[dict] = []

        def worker(operator):
            barrier.wait()
            outcomes.append(self.svc.publish("SC-001", operator=operator))

        threads = [
            threading.Thread(target=worker, args=("甲",)),
            threading.Thread(target=worker, args=("乙",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 相同内容并发发布：事务串行化后只冻结一个课程包，两者指向同一版本
        self.assertEqual({o["id"] for o in outcomes}, {outcomes[0]["id"]})
        self.assertEqual({o["version"] for o in outcomes}, {1})
        self.assertEqual({o["created"] for o in outcomes}, {True, False})


if __name__ == "__main__":
    unittest.main()
