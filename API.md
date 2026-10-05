# HTTP 接口文档

所有请求/响应均为 `application/json; charset=utf-8`。错误统一形如：

```json
{"error": "conflict", "message": "……", "details": {}}
```

状态码：`400` 请求结构错误、`404` 资源不存在、`409` 并发/状态冲突、`422` 图谱校验失败。

## 1. 创建课程（含初始草稿）

`POST /courses`

```json
{
  "code": "WB-001",
  "title": "文博研学一日课",
  "created_by": "统筹员-周",
  "graph": {
    "nodes": [
      {"id": "t1", "type": "topic", "title": "青铜器概览",
       "age_min": 8, "age_max": 14, "materials": [],
       "authorization_valid_until": "2027-12-31"},
      {"id": "p1", "type": "practice", "title": "拓片实践",
       "age_min": 9, "age_max": 14,
       "materials": [{"name": "宣纸", "quantity": 2}],
       "authorization_valid_until": "2027-12-31"},
      {"id": "s1", "type": "safety", "title": "实验室安全须知",
       "age_min": 8, "age_max": 14, "materials": [],
       "authorization_valid_until": "2027-12-31"}
    ],
    "edges": [
      {"source": "t1", "target": "p1"}
    ]
  }
}
```

节点字段：`id`、`type`(topic/practice/safety)、`title`、`age_min/age_max`、
`materials:[{name, quantity}]`、`authorization_valid_until`(YYYY-MM-DD)、
可选 `status`(active/suspended/replaced)。

返回 `201 {"course_id", "draft_version_id"}`。

## 2. 校验草稿（不发布）

`POST /courses/{cid}/versions/{vid}/validate`

```json
{"ok": false,
 "issues": [{"code": "E205", "severity": "error",
             "message": "检测到循环依赖：t1 -> p1 -> t1", "node_id": "t1"}],
 "fingerprint": "…"}
```

## 3. 冻结发布

`POST /courses/{cid}/versions/{vid}/publish`

```json
{"created_by": "统筹员-周", "expected_version_id": "可选：调用方认知的当前版本"}
```

返回版本号、父版本、指纹与课程包年龄交集；校验失败 `422`，并发冲突 `409`。

## 4. 版本链 / 事件

- `POST /courses/{cid}/revisions` —— 基于当前冻结版本生成修订草稿。
- `PUT /courses/{cid}/versions/{vid}/graph` —— 覆盖草稿图谱（仅 draft 可改）。
- `GET /courses/{cid}/versions` —— 完整版本链（含 seq、kind、parent/base/copy 指针）。
- `GET /courses/{cid}/events` —— 草稿创建/发布审计事件。

## 5. 紧急替代

`POST /courses/{cid}/emergency-replace`

```json
{"node_id": "t2", "created_by": "场馆管理员-吴",
 "replacement": {"title": "甲骨文数字体验（应急）",
                 "age_min": 8, "age_max": 12,
                 "materials": [],
                 "authorization_valid_until": "2027-03-01"}}
```

生成 `emergency_replace` 草稿：旧节点 `replaced`+`replaced_by`，新节点 `replaces`，
依赖关系整体迁移。校验通过后 `publish`。

## 6. 局部停用

`POST /courses/{cid}/suspensions`

```json
{"node_id": "t3", "reason": "讲解员临时缺位", "created_by": "统筹员-周"}
```

生成 `suspension` 草稿。若停用节点仍被生效节点依赖，发布会被 `422/E204` 拦截。

## 7. 课程复制

`POST /courses/{cid}/copy` —— `{"code": "WB-001-A", "title": "分校副本"}`。
复制当前冻结包到新课程，独立版本链（seq 重新开始），记录来源版本。

## 8. 读取冻结课程包

`GET /courses/{cid}/packages/{vid}` —— 返回版本元数据、年龄交集、节点/边数与完整图谱。

## 9. 比较课程包

`GET /courses/{cid}/compare?from={version_id}&to={version_id}`
（跨课程加 `&to_course={other_cid}`；`to` 缺省为当前版本）

```json
{"added_nodes": [], "removed_nodes": [],
 "suspended_nodes": [], "replaced_nodes": ["t2"],
 "changed_nodes": [{"node_id": "p1",
                    "fields": {"materials_removed": ["墨汁"]}}],
 "added_edges": [], "removed_edges": [],
 "age_range": {"old": [9, 12], "new": [9, 12], "narrowed": false},
 "breaking": true,
 "reasons": ["replaced_nodes:t2"]}
```

`breaking` 判定：节点删除/停用/替代、材料移除、依赖边删除；纯新增节点不破坏兼容；
年龄区间收窄在 `age_range.narrowed` 提示但不计为破坏性变更。

## 10. 预约与受影响分析

`POST /courses/{cid}/reservations`

```json
{"school_contact": "王老师", "scheduled_date": "2026-11-20",
 "party_size": 30, "version_id": "可选，缺省锁定当前版本"}
```

`GET /courses/{cid}/affected-reservations`：对比每条**未来**已确认预约锁定的版本与
当前冻结版本，输出是否受影响、原因与涉及节点：

```json
{"course_id": "…", "current_version_id": "…", "checked_on": "2026-10-05",
 "total_future": 2, "affected_count": 1,
 "reservations": [
   {"reservation_id": "…", "school_contact": "王老师",
    "scheduled_date": "2026-11-20", "party_size": 30,
    "pinned_version_id": "…", "current_version_id": "…",
    "affected": true, "reasons": ["replaced_nodes:t2"],
    "affected_nodes": ["t2"], "diff": { }}
 ]}
```

预约版本不可变：新版发布后旧预约仍指向旧冻结包，因此差异即"内容负责人更新前置主题后，
已发布包与未来预约不兼容"的显式提示。
