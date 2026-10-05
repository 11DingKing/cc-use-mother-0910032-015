"""课程发布领域的端到端回归测试（零第三方依赖）。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from curriculum_publish.api import build_server
from curriculum_publish.checks import compare_graphs, graph_fingerprint
from curriculum_publish.errors import ConflictError, GraphValidationError
from curriculum_publish.service import CurriculumService
from curriculum_publish.store import Store

TODAY = date(2026, 10, 5)
FUTURE = "2026-11-20"
PAST = "2026-09-01"


def base_graph() -> dict:
    return {
        "nodes": [
            {"id": "t1", "type": "topic", "title": "青铜器概览",
             "age_min": 8, "age_max": 14, "materials": [],
             "authorization_valid_until": "2027-12-31"},
            {"id": "t2", "type": "topic", "title": "甲骨文入门",
             "age_min": 8, "age_max": 12, "materials": [],
             "authorization_valid_until": "2027-06-30"},
            {"id": "t3", "type": "topic", "title": "拓展：古乐欣赏",
             "age_min": 8, "age_max": 14, "materials": [],
             "authorization_valid_until": "2027-06-30"},
            {"id": "p1", "type": "practice", "title": "拓片实践",
             "age_min": 9, "age_max": 14,
             "materials": [{"name": "宣纸", "quantity": 2},
                           {"name": "墨汁", "quantity": 1}],
             "authorization_valid_until": "2027-12-31"},
            {"id": "s1", "type": "safety", "title": "实验室安全须知",
             "age_min": 8, "age_max": 14, "materials": [],
             "authorization_valid_until": "2027-12-31"},
        ],
        "edges": [
            {"source": "t1", "target": "t2"},
            {"source": "t1", "target": "p1"},
            {"source": "t2", "target": "p1"},
        ],
    }


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.service = CurriculumService(self.store)

    def publish_valid(self, graph=None):
        r = self.store.create_course("C-001", "研学课", graph or base_graph())
        return self.store.publish_draft(r["course_id"], r["draft_version_id"], today=TODAY)

    def test_valid_graph_publishes_and_freezes(self):
        out = self.publish_valid()
        self.assertEqual(out["version_seq"], 1)
        self.assertEqual(out["age_range"], [9, 12])
        self.assertEqual(len(out["fingerprint"]), 64)
        manifest = self.store.get_manifest(
            list(self.store.list_courses())[0]["id"], out["version_id"])
        self.assertEqual(manifest["node_count"], 5)
        self.assertEqual(manifest["version"]["status"], "frozen")

    def test_cycle_is_rejected(self):
        graph = base_graph()
        graph["edges"].append({"source": "p1", "target": "t1"})
        r = self.store.create_course("C-CYC", "循环课", graph)
        result = self.store.validate_draft(r["course_id"], r["draft_version_id"], TODAY)
        self.assertFalse(result["ok"])
        self.assertIn("E205", {i["code"] for i in result["issues"]})
        with self.assertRaises(GraphValidationError) as ctx:
            self.store.publish_draft(r["course_id"], r["draft_version_id"], today=TODAY)
        self.assertTrue(any(i["code"] == "E205" for i in ctx.exception.details))

    def test_missing_prerequisite_node(self):
        from curriculum_publish.checks import validate_graph
        graph = base_graph()
        graph["edges"].append({"source": "tX", "target": "p1"})
        codes = {i["code"] for i in validate_graph(graph, TODAY)}
        self.assertIn("E203", codes)

    def test_active_node_depending_on_suspended(self):
        from curriculum_publish.checks import validate_graph
        graph = base_graph()
        graph["nodes"][1]["status"] = "suspended"  # t2 停用，p1 仍依赖它
        codes = {i["code"] for i in validate_graph(graph, TODAY)}
        self.assertIn("E204", codes)

    def test_missing_and_expired_authorization(self):
        from curriculum_publish.checks import validate_graph
        graph = base_graph()
        del graph["nodes"][0]["authorization_valid_until"]
        graph["nodes"][1]["authorization_valid_until"] = "2026-09-01"
        codes = {i["code"] for i in validate_graph(graph, TODAY)}
        self.assertIn("E130", codes)  # 缺失授权期限
        self.assertIn("E131", codes)  # 授权已到期

    def test_practice_requires_materials(self):
        from curriculum_publish.checks import validate_graph
        graph = base_graph()
        graph["nodes"][3]["materials"] = []
        self.assertIn("E124", {i["code"] for i in validate_graph(graph, TODAY)})

    def test_age_intersection_empty(self):
        from curriculum_publish.checks import validate_graph
        graph = base_graph()
        graph["nodes"][3]["age_min"] = 13  # p1 13-14，与 t2 8-12 无交集
        graph["nodes"][3]["age_max"] = 14
        self.assertIn("E208", {i["code"] for i in validate_graph(graph, TODAY)})

    def test_requires_safety_node(self):
        from curriculum_publish.checks import validate_graph
        graph = base_graph()
        graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "safety"]
        self.assertIn("E207", {i["code"] for i in validate_graph(graph, TODAY)})

    def test_frozen_graph_is_immutable(self):
        out = self.publish_valid()
        cid = self.store.list_courses()[0]["id"]
        with self.assertRaises(ConflictError):
            self.store.replace_draft_graph(cid, out["version_id"], base_graph())


class VersionChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.service = CurriculumService(self.store)
        r = self.store.create_course("C-100", "文博研学", base_graph())
        self.cid = r["course_id"]
        self.v1 = self.store.publish_draft(self.cid, r["draft_version_id"], today=TODAY)

    def test_revision_chain(self):
        draft = self.store.create_revision(self.cid)
        graph = self.store.graph_of(self._row(draft))
        graph["nodes"].append({
            "id": "t4", "type": "topic", "title": "新主题：钱币史",
            "age_min": 10, "age_max": 14, "materials": [],
            "authorization_valid_until": "2028-01-01"})
        self.store.replace_draft_graph(self.cid, draft, graph)
        v2 = self.store.publish_draft(self.cid, draft, today=TODAY)
        self.assertEqual(v2["version_seq"], 2)
        self.assertEqual(v2["parent_version_id"], self.v1["version_id"])
        chain = self.store.list_versions(self.cid)
        self.assertEqual([v["version_seq"] for v in chain if v["status"] == "frozen"], [1, 2])
        self.assertEqual([v["kind"] for v in chain], ["initial", "revision"])

    def _row(self, vid):
        conn = self.store._connect()
        try:
            return self.store.get_version(conn, self.cid, vid)
        finally:
            if self.store._mem is None:
                conn.close()

    def test_emergency_replace_chain(self):
        draft = self.store.emergency_replace(self.cid, "t2", {
            "title": "甲骨文数字体验（替代）",
            "authorization_valid_until": "2027-03-01",
        })
        v = self.store.publish_draft(self.cid, draft, today=TODAY)
        graph = self.store.graph_of(self._row(v["version_id"]))
        status = {n["id"]: n.get("status", "active") for n in graph["nodes"]}
        self.assertEqual(status["t2"], "replaced")
        new_ids = [nid for nid, s in status.items()
                   if s == "active" and nid not in {"t1", "t3", "p1", "s1"}]
        self.assertEqual(len(new_ids), 1)
        # p1 的入依赖已切到替代节点
        sources = {e["source"] for e in graph["edges"] if e["target"] == "p1"}
        self.assertIn(new_ids[0], sources)
        self.assertNotIn("t2", sources)
        row = self._row(v["version_id"])
        self.assertEqual(row["kind"], "emergency_replace")
        self.assertEqual(row["parent_version_id"], self.v1["version_id"])

    def test_suspension_chain(self):
        draft = self.store.suspend_node(self.cid, "t3", "讲解员临时缺位")
        v = self.store.publish_draft(self.cid, draft, today=TODAY)
        graph = self.store.graph_of(self._row(v["version_id"]))
        t3 = next(n for n in graph["nodes"] if n["id"] == "t3")
        self.assertEqual(t3["status"], "suspended")
        self.assertEqual(self._row(v["version_id"])["kind"], "suspension")

    def test_suspension_of_prerequisite_blocks_publish(self):
        # t2 是 p1 的前置，停用后必须先修复图谱才能发布
        draft = self.store.suspend_node(self.cid, "t2", "授权争议")
        with self.assertRaises(GraphValidationError) as ctx:
            self.store.publish_draft(self.cid, draft, today=TODAY)
        self.assertTrue(any(i["code"] == "E204" for i in ctx.exception.details))

    def test_copy_course_starts_separate_chain(self):
        out = self.store.copy_course(self.cid, "C-100-COPY", "文博研学（分校副本）")
        v = self.store.publish_draft(out["course_id"], out["draft_version_id"], today=TODAY)
        self.assertEqual(v["version_seq"], 1)
        self.assertEqual(v["parent_version_id"], None)
        row = self._copy_row(out["course_id"], v["version_id"])
        self.assertEqual(row["kind"], "copy")
        self.assertEqual(row["copy_from_version_id"], self.v1["version_id"])
        # 原课程版本链不受影响
        self.assertEqual(len(self.store.list_versions(self.cid)), 1)

    def _copy_row(self, new_cid, vid):
        conn = self.store._connect()
        try:
            return self.store.get_version(conn, new_cid, vid)
        finally:
            if self.store._mem is None:
                conn.close()

    def test_concurrent_publish_conflict(self):
        # 两个草稿都基于 v1，只有一个能发布成功
        draft_a = self.store.create_revision(self.cid)
        draft_b = self.store.create_revision(self.cid)
        ga = self.store.graph_of(self._row(draft_a))
        ga["nodes"][0]["title"] = "青铜器概览（修订A）"
        self.store.replace_draft_graph(self.cid, draft_a, ga)
        self.store.publish_draft(self.cid, draft_a, today=TODAY)

        with self.assertRaises(ConflictError) as ctx:
            self.store.publish_draft(self.cid, draft_b, today=TODAY)
        self.assertEqual(ctx.exception.code, "conflict")
        self.assertIn("current_version_id", ctx.exception.details)

    def test_identical_graph_publish_conflict(self):
        draft = self.store.create_revision(self.cid)
        with self.assertRaises(ConflictError):
            self.store.publish_draft(self.cid, draft, today=TODAY)


class CompareAndReservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.service = CurriculumService(self.store)
        r = self.store.create_course("C-200", "文博研学", base_graph())
        self.cid = r["course_id"]
        self.v1 = self.store.publish_draft(self.cid, r["draft_version_id"], today=TODAY)
        self.r1 = self.store.create_reservation(
            self.cid, None, "王老师", FUTURE, 30)["reservation_id"]
        self.r_past = self.store.create_reservation(
            self.cid, None, "李老师", PAST, 20)["reservation_id"]

    def publish_v2_with_new_topic(self):
        draft = self.store.create_revision(self.cid)
        return self.store.publish_draft(self.cid, draft, today=TODAY)

    def test_compare_detects_emergency_replacement(self):
        draft = self.store.emergency_replace(self.cid, "t2", {
            "title": "替代主题", "authorization_valid_until": "2027-03-01"})
        v2 = self.store.publish_draft(self.cid, draft, today=TODAY)
        diff = self.service.compare_packages(
            self.cid, self.v1["version_id"], self.cid, v2["version_id"])
        self.assertTrue(diff["breaking"])
        self.assertIn("t2", diff["replaced_nodes"])
        self.assertTrue(any(r.startswith("replaced_nodes") for r in diff["reasons"]))

    def test_compare_detects_material_removal(self):
        old = base_graph()
        new = base_graph()
        new["nodes"][3]["materials"] = [{"name": "宣纸", "quantity": 2}]  # 墨汁被移除
        d = compare_graphs(old, new)
        self.assertTrue(d["breaking"])
        self.assertEqual(
            d["changed_nodes"][0]["fields"]["materials_removed"], ["墨汁"])

    def test_compare_non_breaking_addition(self):
        old = base_graph()
        new = base_graph()
        new["nodes"].append({"id": "t9", "type": "topic", "title": "新增",
                             "age_min": 9, "age_max": 12, "materials": [],
                             "authorization_valid_until": "2028-01-01"})
        d = compare_graphs(old, new)
        self.assertFalse(d["breaking"])
        self.assertEqual(d["added_nodes"], ["t9"])

    def test_affected_future_reservations_listed(self):
        draft = self.store.emergency_replace(self.cid, "t2", {
            "title": "替代主题", "authorization_valid_until": "2027-03-01"})
        self.store.publish_draft(self.cid, draft, today=TODAY)
        report = self.service.affected_future_reservations(self.cid, TODAY)
        self.assertEqual(report["total_future"], 1)  # 过去的预约不在未来列表
        self.assertEqual(report["affected_count"], 1)
        item = report["reservations"][0]
        self.assertTrue(item["affected"])
        self.assertIn("t2", item["affected_nodes"])
        self.assertEqual(item["reservation_id"], self.r1)

    def test_reservation_on_current_version_not_affected(self):
        draft = self.store.emergency_replace(self.cid, "t2", {
            "title": "替代主题", "authorization_valid_until": "2027-03-01"})
        v2 = self.store.publish_draft(self.cid, draft, today=TODAY)
        self.store.create_reservation(self.cid, v2["version_id"], "赵老师", "2026-12-05", 40)
        report = self.service.affected_future_reservations(self.cid, TODAY)
        by_id = {r["reservation_id"]: r for r in report["reservations"]}
        self.assertFalse(list(by_id.values())[-1]["affected"])

    def test_fingerprint_deterministic(self):
        g1 = base_graph()
        g2 = base_graph()
        g2["nodes"] = list(reversed(g2["nodes"]))
        self.assertEqual(graph_fingerprint(g1), graph_fingerprint(g2))


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = build_server(":memory:", "127.0.0.1", 0, verbose=False)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        conn.request(method, path, body,
                     {"Content-Type": "application/json; charset=utf-8"} if body is not None else {})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw) if raw else {}

    def test_full_flow_over_http(self):
        status, out = self.call("POST", "/courses",
                                {"code": "H-1", "title": "HTTP 课", "graph": base_graph()})
        self.assertEqual(status, 201)
        cid, vid = out["course_id"], out["draft_version_id"]

        status, out = self.call("POST", f"/courses/{cid}/versions/{vid}/validate")
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])

        status, pub = self.call("POST", f"/courses/{cid}/versions/{vid}/publish")
        self.assertEqual(status, 200)
        self.assertEqual(pub["version_seq"], 1)

        status, resv = self.call("POST", f"/courses/{cid}/reservations",
                                 {"school_contact": "王老师",
                                  "scheduled_date": FUTURE, "party_size": 25})
        self.assertEqual(status, 201)

        # 紧急替代并发布
        status, repl = self.call("POST", f"/courses/{cid}/emergency-replace",
                                 {"node_id": "t2",
                                  "replacement": {"title": "替代",
                                                  "authorization_valid_until": "2027-03-01"}})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", f"/courses/{cid}/versions/{repl['draft_version_id']}/publish")
        self.assertEqual(status, 200)

        status, report = self.call("GET", f"/courses/{cid}/affected-reservations")
        self.assertEqual(status, 200)
        self.assertEqual(report["affected_count"], 1)

        status, chain = self.call("GET", f"/courses/{cid}/versions")
        self.assertEqual(status, 200)
        kinds = [v["kind"] for v in chain["versions"]]
        self.assertEqual(kinds, ["initial", "emergency_replace"])

        # 比较接口
        status, diff = self.call(
            "GET", f"/courses/{cid}/compare?from={pub['version_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(diff["breaking"])

    def test_cycle_returns_422_over_http(self):
        graph = base_graph()
        graph["edges"].append({"source": "p1", "target": "t1"})
        _, out = self.call("POST", "/courses",
                           {"code": "H-CYC", "title": "坏课", "graph": graph})
        status, err = self.call(
            "POST", f"/courses/{out['course_id']}/versions/{out['draft_version_id']}/publish")
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "graph_invalid")
        self.assertTrue(any(d["code"] == "E205" for d in err["details"]))

    def test_concurrent_publish_second_writer_gets_409(self):
        _, out = self.call("POST", "/courses",
                           {"code": "H-2", "title": "并发课", "graph": base_graph()})
        cid = out["course_id"]
        self.call("POST", f"/courses/{cid}/versions/{out['draft_version_id']}/publish")
        _, da = self.call("POST", f"/courses/{cid}/revisions")
        _, db = self.call("POST", f"/courses/{cid}/revisions")
        graph = base_graph()
        graph["nodes"][0]["title"] = "青铜器概览（修订A）"
        sa, _ = self.call("PUT", f"/courses/{cid}/versions/{da['draft_version_id']}/graph",
                          {"graph": graph})
        self.assertEqual(sa, 200)
        s1, _ = self.call("POST", f"/courses/{cid}/versions/{da['draft_version_id']}/publish")
        self.assertEqual(s1, 200)
        s2, err = self.call("POST", f"/courses/{cid}/versions/{db['draft_version_id']}/publish")
        self.assertEqual(s2, 409)
        self.assertEqual(err["error"], "conflict")


if __name__ == "__main__":
    unittest.main()
