"""离线演示：不启动 HTTP 服务，直接跑通发布、替代、比较、影响分析。

用法：python3 tools/seed_demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from curriculum_publish.service import CurriculumService
from curriculum_publish.store import Store

TODAY = date(2026, 10, 5)

GRAPH = {
    "nodes": [
        {"id": "t1", "type": "topic", "title": "青铜器概览", "age_min": 8, "age_max": 14,
         "materials": [], "authorization_valid_until": "2027-12-31"},
        {"id": "t2", "type": "topic", "title": "甲骨文入门", "age_min": 8, "age_max": 12,
         "materials": [], "authorization_valid_until": "2027-06-30"},
        {"id": "p1", "type": "practice", "title": "拓片实践", "age_min": 9, "age_max": 14,
         "materials": [{"name": "宣纸", "quantity": 2}, {"name": "墨汁", "quantity": 1}],
         "authorization_valid_until": "2027-12-31"},
        {"id": "s1", "type": "safety", "title": "实验室安全须知", "age_min": 8, "age_max": 14,
         "materials": [], "authorization_valid_until": "2027-12-31"},
    ],
    "edges": [{"source": "t1", "target": "t2"},
              {"source": "t1", "target": "p1"},
              {"source": "t2", "target": "p1"}],
}


def main() -> None:
    store = Store()
    service = CurriculumService(store)

    created = store.create_course("DEMO-1", "文博研学一日课", GRAPH, actor="统筹员")
    cid = created["course_id"]
    v1 = store.publish_draft(cid, created["draft_version_id"], today=TODAY)
    print("v1 已冻结 seq=%s 年龄=%s" % (v1["version_seq"], v1["age_range"]))

    store.create_reservation(cid, v1["version_id"], "王老师", "2026-11-20", 30)

    draft = store.emergency_replace(cid, "t2", {
        "title": "甲骨文数字体验（应急）",
        "authorization_valid_until": "2027-03-01",
    }, actor="场馆管理员")
    v2 = store.publish_draft(cid, draft, today=TODAY)
    print("v2 紧急替代 seq=%s parent=%s" % (v2["version_seq"], v2["parent_version_id"][:8]))

    diff = service.compare_packages(cid, v1["version_id"], cid, v2["version_id"])
    print("包比较 breaking=%s reasons=%s" % (diff["breaking"], diff["reasons"]))

    report = service.affected_future_reservations(cid, TODAY)
    print("未来预约 %s 条，受影响 %s 条" % (report["total_future"], report["affected_count"]))
    print(json.dumps(report["reservations"], ensure_ascii=False, indent=2))

    print("版本链：")
    for v in store.list_versions(cid):
        print("  seq=%s kind=%s status=%s id=%s" % (
            v["version_seq"], v["kind"], v["status"], v["id"][:8]))


if __name__ == "__main__":
    main()
