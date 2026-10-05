"""课程依赖图谱的纯函数校验、冻结哈希与版本比较。

节点类型：
- topic    讲解主题
- practice 实践环节
- safety   安全说明

边方向：source 是 target 的前置依赖（source -> target 表示 target 依赖 source）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

NODE_TYPES = ("topic", "practice", "safety")
ACTIVE_STATUSES = ("active",)


def issue(code: str, message: str, node_id: str | None = None) -> dict[str, Any]:
    """构造一条校验问题。"""
    value: dict[str, Any] = {"code": code, "severity": "error", "message": message}
    if node_id is not None:
        value["node_id"] = node_id
    return value


def active_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [n for n in graph.get("nodes", []) if n.get("status", "active") == "active"]


def effective_age(graph: dict[str, Any]) -> tuple[int | None, int | None]:
    """课程包适用年龄取所有生效节点年龄区间的交集。"""
    nodes = active_nodes(graph)
    mins = [n.get("age_min") for n in nodes if isinstance(n.get("age_min"), int)]
    maxs = [n.get("age_max") for n in nodes if isinstance(n.get("age_max"), int)]
    if not mins or not maxs:
        return None, None
    return max(mins), min(maxs)


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        y, m, d = value.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def validate_graph(graph: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """发布前完整校验：循环、缺失条件、授权期限等。"""
    issues: list[dict[str, Any]] = []
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    seen_ids: set[str] = set()
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            issues.append(issue("E100", "节点缺少唯一编号"))
            continue
        if nid in seen_ids:
            issues.append(issue("E101", f"节点编号重复：{nid}", nid))
            continue
        seen_ids.add(nid)
        node_map[nid] = node

        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            issues.append(issue("E102", f"节点类型非法：{ntype!r}，允许 {NODE_TYPES}", nid))
        if not isinstance(node.get("title"), str) or not node.get("title"):
            issues.append(issue("E103", "节点缺少标题", nid))

        is_active = node.get("status", "active") == "active"
        amin, amax = node.get("age_min"), node.get("age_max")
        if is_active:
            if not isinstance(amin, int) or not isinstance(amax, int):
                issues.append(issue("E110", "适用年龄缺失，必须提供 age_min/age_max", nid))
            elif not (0 <= amin <= amax <= 120):
                issues.append(issue("E111", f"年龄区间非法：{amin}-{amax}", nid))

            materials = node.get("materials", [])
            if not isinstance(materials, list):
                issues.append(issue("E120", "必备材料必须是列表", nid))
            else:
                names: set[str] = set()
                for mat in materials:
                    name = mat.get("name") if isinstance(mat, dict) else None
                    qty = mat.get("quantity") if isinstance(mat, dict) else None
                    if not isinstance(name, str) or not name:
                        issues.append(issue("E121", "存在缺少名称的材料", nid))
                    elif name in names:
                        issues.append(issue("E122", f"材料重复登记：{name}", nid))
                    else:
                        names.add(name)
                    if not isinstance(qty, int) or qty <= 0:
                        issues.append(issue("E123", f"材料 {name!r} 数量必须为正整数", nid))
                if ntype == "practice" and not materials:
                    issues.append(issue("E124", "实践环节必须登记必备材料", nid))

            expires = _parse_iso_date(node.get("authorization_valid_until"))
            if expires is None:
                issues.append(issue("E130", "缺少授权期限", nid))
            elif expires < today:
                issues.append(issue(
                    "E131",
                    f"授权已于 {expires.isoformat()} 到期（今天 {today.isoformat()}）",
                    nid,
                ))

    # 边校验
    seen_edges: set[tuple[str, str]] = set()
    active_ids = {nid for nid, n in node_map.items() if n.get("status", "active") == "active"}
    for edge in edges:
        src, dst = edge.get("source"), edge.get("target")
        if not isinstance(src, str) or not isinstance(dst, str):
            issues.append(issue("E200", f"依赖边缺少端点：{edge!r}"))
            continue
        if src == dst:
            issues.append(issue("E201", f"节点自依赖：{src}", src))
        pair = (src, dst)
        if pair in seen_edges:
            issues.append(issue("E202", f"重复依赖：{src} -> {dst}", dst))
            continue
        seen_edges.add(pair)
        if src not in node_map:
            issues.append(issue("E203", f"依赖起点不存在：{src}（缺失前置条件）", dst))
        if dst not in node_map:
            issues.append(issue("E203", f"依赖终点不存在：{dst}（缺失前置条件）", src))
        if src in node_map and dst in node_map:
            # 生效节点依赖已停用/替换节点 -> 前置条件缺失
            if dst in active_ids and src not in active_ids:
                issues.append(issue(
                    "E204",
                    f"生效节点 {dst} 仍依赖已停用/替换节点 {src}",
                    dst,
                ))

    # 循环依赖检测（仅在端点存在的边上）
    adjacency: dict[str, set[str]] = {nid: set() for nid in node_map}
    for src, dst in seen_edges:
        if src in adjacency and dst in adjacency:
            adjacency[src].add(dst)
    cycle = _find_cycle(adjacency)
    if cycle:
        issues.append(issue("E205", "检测到循环依赖：" + " -> ".join(cycle), cycle[0]))

    # 实践环节必须直接依赖至少一个讲解主题
    for nid, node in node_map.items():
        if node.get("type") == "practice" and node.get("status", "active") == "active":
            has_topic = any(
                src in node_map and node_map[src].get("type") == "topic"
                and src in active_ids
                for src, dst in seen_edges
                if dst == nid
            )
            if not has_topic:
                issues.append(issue("E206", "实践环节缺少讲解主题作为直接前置", nid))

    # 课程包必须包含生效的安全说明
    if not any(n.get("type") == "safety" for n in active_nodes({"nodes": list(node_map.values())})):
        issues.append(issue("E207", "课程包必须包含至少一条生效的安全说明"))

    # 适用年龄交集
    age_min, age_max = effective_age({"nodes": list(node_map.values())})
    if node_map and age_min is not None and age_min > age_max:
        issues.append(issue(
            "E208",
            f"各主题年龄区间无交集，无法确定课程包适用年龄（交集下界 {age_min} > 上界 {age_max}）",
        ))

    return sorted(issues, key=lambda item: (item["code"], item.get("node_id") or ""))


def _find_cycle(adjacency: dict[str, set[str]]) -> list[str] | None:
    """DFS 返回一条环（含回到起点的闭合路径）。"""
    color: dict[str, int] = {nid: 0 for nid in adjacency}  # 0 白 1 灰 2 黑
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(adjacency[node]):
            if color[nxt] == 0:
                found = visit(nxt)
                if found:
                    return found
            elif color[nxt] == 1:
                start = stack.index(nxt)
                return stack[start:] + [nxt]
        stack.pop()
        color[node] = 2
        return None

    for nid in sorted(adjacency):
        if color[nid] == 0:
            found = visit(nid)
            if found:
                return found
    return None


def graph_fingerprint(graph: dict[str, Any]) -> str:
    """对完整图谱做确定性指纹，冻结时一并存档。"""
    payload = {
        "nodes": sorted(graph.get("nodes", []), key=lambda n: n.get("id", "")),
        "edges": sorted(
            graph.get("edges", []),
            key=lambda e: (e.get("source", ""), e.get("target", "")),
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compare_graphs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """比较两个冻结图谱（方向：old -> new）。"""
    old_nodes = {n["id"]: n for n in old.get("nodes", [])}
    new_nodes = {n["id"]: n for n in new.get("nodes", [])}

    added = sorted(nid for nid in new_nodes if nid not in old_nodes)
    removed: list[str] = []
    suspended: list[str] = []
    replaced: list[str] = []
    changed: list[dict[str, Any]] = []

    for nid, on in old_nodes.items():
        nn = new_nodes.get(nid)
        if nn is None:
            if on.get("status", "active") == "active":
                removed.append(nid)
            continue
        old_active = on.get("status", "active") == "active"
        new_status = nn.get("status", "active")
        if old_active and new_status == "suspended":
            suspended.append(nid)
        if old_active and (new_status == "replaced" or nn.get("replaces") == nid):
            replaced.append(nid)
        fields: dict[str, Any] = {}
        for key in ("title", "age_min", "age_max", "authorization_valid_until"):
            if on.get(key) != nn.get(key):
                fields[key] = {"old": on.get(key), "new": nn.get(key)}
        old_mats = {m["name"]: m["quantity"] for m in on.get("materials", [])}
        new_mats = {m["name"]: m["quantity"] for m in nn.get("materials", [])}
        removed_materials = sorted(name for name in old_mats if name not in new_mats)
        quantity_changed = {
            name: {"old": old_mats[name], "new": new_mats[name]}
            for name in sorted(old_mats)
            if name in new_mats and old_mats[name] != new_mats[name]
        }
        if removed_materials:
            fields["materials_removed"] = removed_materials
        if quantity_changed:
            fields["materials_quantity"] = quantity_changed
        if fields:
            changed.append({"node_id": nid, "fields": fields})

    old_edges = {(e["source"], e["target"]) for e in old.get("edges", [])}
    new_edges = {(e["source"], e["target"]) for e in new.get("edges", [])}
    # 紧急替代把旧节点的边整体重挂到替代节点；按 replaces 映射后，这类变化不算删除依赖
    redirect = {n["replaces"]: n["id"]
                for n in new_nodes.values() if n.get("replaces")}

    def redirected(pair: tuple[str, str]) -> tuple[str, str]:
        return (redirect.get(pair[0], pair[0]), redirect.get(pair[1], pair[1]))

    rewired = {(s, t) for s, t in (old_edges - new_edges)
               if redirected((s, t)) in new_edges}
    edges_added = sorted(new_edges - old_edges)
    edges_removed = sorted((old_edges - new_edges) - rewired)

    omin, omax = old.get("age_min"), old.get("age_max")
    if omin is None:
        omin, omax = effective_age(old)
    nmin, nmax = new.get("age_min"), new.get("age_max")
    if nmin is None:
        nmin, nmax = effective_age(new)
    age_narrowed = (
        isinstance(omin, int) and isinstance(omax, int)
        and isinstance(nmin, int) and isinstance(nmax, int)
        and (nmin > omin or nmax < omax)
    )

    reasons: list[str] = []
    if removed:
        reasons.append("removed_nodes:" + ",".join(removed))
    if suspended:
        reasons.append("suspended_nodes:" + ",".join(suspended))
    if replaced:
        reasons.append("replaced_nodes:" + ",".join(replaced))
    material_loss = [
        f"{item['node_id']}:{','.join(item['fields'].get('materials_removed', []))}"
        for item in changed
        if item["fields"].get("materials_removed")
    ]
    if material_loss:
        reasons.append("removed_materials:" + ";".join(material_loss))
    if edges_removed:
        reasons.append("removed_dependencies")
    if age_narrowed:
        reasons.append("age_range_narrowed")

    return {
        "added_nodes": added,
        "removed_nodes": sorted(removed),
        "suspended_nodes": sorted(suspended),
        "replaced_nodes": sorted(replaced),
        "changed_nodes": changed,
        "added_edges": [{"source": s, "target": t} for s, t in edges_added],
        "removed_edges": [{"source": s, "target": t} for s, t in edges_removed],
        "rewired_edges": [{"source": s, "target": t} for s, t in sorted(rewired)],
        "age_range": {"old": [omin, omax], "new": [nmin, nmax], "narrowed": age_narrowed},
        "breaking": bool(
            removed or suspended or replaced or material_loss or edges_removed
        ),
        "reasons": reasons,
    }
