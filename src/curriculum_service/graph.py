"""课程依赖图谱：规范化、发布前校验与内容指纹。

节点结构（JSON 字典）::

    {
      "id": "T1",
      "type": "topic | practice | safety",
      "title": "主题名称",
      "age_min": 6, "age_max": 12,            # 适用年龄（闭区间）
      "depends_on": ["T0"],                    # 前置主题/环节
      "required_materials": ["安全帽"],         # 必备材料
      "license": {"licensed_to": "场馆A",
                  "valid_from": "2026-01-01",
                  "valid_until": "2027-01-01"}
    }

课程结构::

    {
      "code": "SC-001", "title": "...",
      "age_min": 7, "age_max": 12,             # 课程整体适用年龄
      "available_materials": ["安全帽", ...],   # 可选：已备材料清单
      "offering_until": "2026-12-31",          # 可选：计划开课截止日
      "nodes": [...]
    }
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from .errors import ValidationIssue

NODE_TYPES = {"topic", "practice", "safety"}


def _parse_date(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 字符串")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field} 日期格式无效：{value}") from exc


def normalize_course(payload: dict[str, Any]) -> dict[str, Any]:
    """校验课程结构并返回补齐默认字段后的副本（不做图规则校验）。"""
    if not isinstance(payload, dict):
        raise ValueError("课程必须是 JSON 对象")
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("课程缺少 code")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("课程缺少 title")

    course: dict[str, Any] = {
        "code": code.strip(),
        "title": title.strip(),
        "age_min": payload.get("age_min"),
        "age_max": payload.get("age_max"),
        "available_materials": list(payload.get("available_materials") or []),
        "offering_until": payload.get("offering_until"),
        "nodes": [],
    }
    for bound in ("age_min", "age_max"):
        value = course[bound]
        if value is not None and not isinstance(value, int):
            raise ValueError(f"{bound} 必须是整数")
    if (
        course["age_min"] is not None
        and course["age_max"] is not None
        and course["age_min"] > course["age_max"]
    ):
        raise ValueError("课程 age_min 不能大于 age_max")
    _parse_date(course["offering_until"], "offering_until")

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("课程 nodes 必须是非空列表")

    seen: set[str] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("节点必须是 JSON 对象")
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("节点缺少 id")
        node_id = node_id.strip()
        if node_id in seen:
            raise ValueError(f"节点 id 重复：{node_id}")
        seen.add(node_id)

        node_type = raw.get("type", "topic")
        if node_type not in NODE_TYPES:
            raise ValueError(f"节点 {node_id} 类型无效：{node_type}")
        title_ = raw.get("title")
        if not isinstance(title_, str) or not title_.strip():
            raise ValueError(f"节点 {node_id} 缺少 title")

        age_min = raw.get("age_min")
        age_max = raw.get("age_max")
        if not isinstance(age_min, int) or not isinstance(age_max, int):
            raise ValueError(f"节点 {node_id} 的 age_min/age_max 必须是整数")
        if age_min > age_max:
            raise ValueError(f"节点 {node_id} 的 age_min 不能大于 age_max")

        depends_on = raw.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(
            isinstance(d, str) and d.strip() for d in depends_on
        ):
            raise ValueError(f"节点 {node_id} 的 depends_on 必须是字符串列表")

        materials = raw.get("required_materials") or []
        if not isinstance(materials, list) or not all(
            isinstance(m, str) and m.strip() for m in materials
        ):
            raise ValueError(f"节点 {node_id} 的 required_materials 必须是字符串列表")

        license_raw = raw.get("license") or {}
        if not isinstance(license_raw, dict):
            raise ValueError(f"节点 {node_id} 的 license 必须是对象")
        valid_from = _parse_date(license_raw.get("valid_from"), f"{node_id}.license.valid_from")
        valid_until = _parse_date(license_raw.get("valid_until"), f"{node_id}.license.valid_until")
        if valid_from and valid_until and valid_from > valid_until:
            raise ValueError(f"节点 {node_id} 授权开始日晚于到期日")

        course["nodes"].append(
            {
                "id": node_id,
                "type": node_type,
                "title": title_.strip(),
                "age_min": age_min,
                "age_max": age_max,
                "depends_on": [d.strip() for d in depends_on],
                "required_materials": [m.strip() for m in materials],
                "license": {
                    "licensed_to": str(license_raw.get("licensed_to") or "").strip(),
                    "valid_from": valid_from.isoformat() if valid_from else None,
                    "valid_until": valid_until.isoformat() if valid_until else None,
                },
            }
        )
    return course


def find_cycle(nodes: list[dict[str, Any]]) -> list[str] | None:
    """返回首个环的节点 id 序列（首尾相同），无环返回 None。"""
    adjacency = {n["id"]: list(n["depends_on"]) for n in nodes}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in adjacency}
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        color[node_id] = GRAY
        stack.append(node_id)
        for dep in adjacency[node_id]:
            if dep not in adjacency:
                continue  # 缺失依赖由 validate_course 报告
            if color[dep] == GRAY:
                start = stack.index(dep)
                return stack[start:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node_id] = BLACK
        return None

    for nid in adjacency:
        if color[nid] == WHITE:
            found = visit(nid)
            if found:
                return found
    return None


def validate_course(
    course: dict[str, Any], *, as_of: date | None = None
) -> list[ValidationIssue]:
    """发布前全量校验：循环、缺失条件、适用年龄、必备材料、授权期限。"""
    as_of = as_of or date.today()
    issues: list[ValidationIssue] = []
    nodes = course["nodes"]
    by_id = {n["id"]: n for n in nodes}

    # 1. 循环依赖
    cycle = find_cycle(nodes)
    if cycle:
        issues.append(
            ValidationIssue(
                "cycle",
                "课程依赖存在循环：" + " -> ".join(cycle),
                cycle=cycle,
            )
        )

    # 2. 缺失前置条件（依赖指向不存在的节点；实践环节要求至少一个前置）
    for node in nodes:
        for dep in node["depends_on"]:
            if dep not in by_id:
                issues.append(
                    ValidationIssue(
                        "missing_dependency",
                        f"节点 {node['id']} 依赖的前置 {dep} 不存在",
                        node=node["id"],
                        dependency=dep,
                    )
                )
        if node["type"] == "practice" and not node["depends_on"]:
            issues.append(
                ValidationIssue(
                    "missing_prerequisite",
                    f"实践环节 {node['id']} 缺少前置主题",
                    node=node["id"],
                )
            )

    # 3. 适用年龄冲突（节点年龄区间必须与课程目标年龄有交集）
    c_min, c_max = course["age_min"], course["age_max"]
    if c_min is not None and c_max is not None:
        for node in nodes:
            if node["age_min"] > c_max or node["age_max"] < c_min:
                issues.append(
                    ValidationIssue(
                        "age_conflict",
                        f"节点 {node['id']} 适用年龄 {node['age_min']}-{node['age_max']} "
                        f"与课程目标年龄 {c_min}-{c_max} 无交集",
                        node=node["id"],
                        course_age=[c_min, c_max],
                        node_age=[node["age_min"], node["age_max"]],
                    )
                )

    # 4. 必备材料缺口（声明 available_materials 时做覆盖核对）
    available = set(course["available_materials"])
    if available:
        for node in nodes:
            missing = [m for m in node["required_materials"] if m not in available]
            if missing:
                issues.append(
                    ValidationIssue(
                        "missing_material",
                        f"节点 {node['id']} 缺少必备材料：{ '、'.join(missing) }",
                        node=node["id"],
                        materials=missing,
                    )
                )

    # 5. 授权期限（发布时点必须有效；若声明开课窗口，须覆盖窗口末端）
    window_end = _parse_date(course.get("offering_until"), "offering_until")
    for node in nodes:
        lic = node["license"]
        licensed_to, vf, vu = lic["licensed_to"], lic["valid_from"], lic["valid_until"]
        if not licensed_to or not vf or not vu:
            issues.append(
                ValidationIssue(
                    "missing_license",
                    f"节点 {node['id']} 缺少完整授权信息",
                    node=node["id"],
                )
            )
            continue
        valid_from = date.fromisoformat(vf)
        valid_until = date.fromisoformat(vu)
        check_date = window_end if window_end and window_end > as_of else as_of
        if check_date < valid_from:
            issues.append(
                ValidationIssue(
                    "license_not_active",
                    f"节点 {node['id']} 授权 {vf} 才生效，{check_date} 尚未生效",
                    node=node["id"],
                    valid_from=vf,
                    valid_until=vu,
                    checked_at=check_date.isoformat(),
                )
            )
        if check_date > valid_until:
            issues.append(
                ValidationIssue(
                    "license_expired",
                    f"节点 {node['id']} 授权已于 {vu} 到期",
                    node=node["id"],
                    valid_from=vf,
                    valid_until=vu,
                    checked_at=check_date.isoformat(),
                )
            )
    return issues


def content_fingerprint(course: dict[str, Any]) -> str:
    """对规范化后的完整图谱计算稳定指纹（SHA-256，前 16 位）。"""
    canon = json.dumps(course, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
