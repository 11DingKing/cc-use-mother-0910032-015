# 主题课程依赖发布

学校研学课程由讲解主题、实践环节和安全说明构成。本项目在领域契约之上提供 Python 后端，
维护**课程版本、主题依赖、适用年龄与必备材料**；发布前检测**循环依赖、缺失条件、授权期限**，
通过后冻结完整图谱；紧急替代、局部停用、课程复制与并发发布形成明确版本链；接口可比较课程包
并列出受影响的未来预约。

## 领域模型

```
课程 courses ──1:N── 版本 versions（draft 草稿 / frozen 冻结）
                       │  parent_version_id   版本链（父版本）
                       │  base_version_id     草稿基于的版本（并发控制依据）
                       │  copy_from_version_id 复制来源（跨课程链）
                       │  fingerprint         完整图谱 SHA-256 指纹
                       └─ graph_json：{nodes, edges} 冻结后不可变
预约 reservations ── 锁定创建时的冻结版本，后续新版本不影响已存预约
事件 events ── 草稿创建、发布的完整审计时间线
```

- 节点类型：`topic`（讲解主题）、`practice`（实践环节）、`safety`（安全说明）。
- 边方向：`source -> target` 表示 **target 依赖 source**（source 是前置）。
- 课程包适用年龄 = 全部生效节点年龄区间的**交集**（交集为空禁止发布）。
- 必备材料按节点登记，实践环节必须有材料，材料需名称与正整数数量。
- 每个生效节点必须提供未到期的 `authorization_valid_until`。

## 发布前校验（错误码）

| 类别 | 错误码 | 含义 |
|---|---|---|
| 循环 | E205 | 依赖图存在环，返回环路径 |
| 缺失条件 | E203/E204/E206 | 依赖端点不存在；生效节点依赖已停用/替代节点；实践环节缺少讲解主题前置 |
| 授权期限 | E130/E131 | 缺少授权期限；授权已到期 |
| 适用年龄 | E110/E111/E208 | 年龄缺失；区间非法；各节点年龄无交集 |
| 必备材料 | E120-E124 | 材料结构/数量非法、重复登记；实践环节无材料 |
| 完整性 | E100-E103/E201/E202/E207 | 节点编号缺失或重复、类型非法、自依赖/重复依赖、缺少安全说明 |

校验不通过发布返回 `422` 并列明全部问题；**图谱通过校验后以 SHA-256 指纹整体冻结**，
冻结版本只读，任何修改都产生新版本。

## 版本链语义

- `initial` 初始发布；`revision` 常规修订（父版本为当前冻结版本）。
- `emergency_replace` 紧急替代：旧节点保留并标记 `replaced` + `replaced_by`，
  新节点带 `replaces`，旧节点的入边/出边全部改挂到替代节点，关系不悬空。
- `suspension` 局部停用：节点标记 `suspended` 与停用原因；若它仍是生效节点的前置，
  发布会被 E204 拦截，必须先修复依赖（如先做紧急替代）。
- `copy` 课程复制：复制当前冻结包，新课程从 seq=1 起独立成链，并记录 `copy_from_version_id`。
- 并发发布：草稿记录其基线版本；发布时若课程当前版本已被别人推进，返回 `409`
  （`expected_version_id` / `base_version_id` 失配）。图谱与某冻结版本完全一致同样拒绝重复发布。

## 接口

详见 [API.md](API.md)。零第三方依赖，基于标准库 `http.server` + SQLite（`BEGIN IMMEDIATE`）。

```bash
PYTHONPATH=src python3 -m curriculum_publish.api --port 8080 --db curriculum.db
```

核心路由：`POST /courses`、`POST /courses/{id}/revisions`、
`PUT .../versions/{vid}/graph`、`POST .../validate`、`POST .../publish`、
`POST .../emergency-replace`、`POST .../suspensions`、`POST .../copy`、
`GET .../packages/{vid}`、`GET .../compare?from=&to=`、
`GET .../affected-reservations`、`POST .../reservations`、`GET .../versions`、`GET .../events`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/curriculum_publish/`：后端服务
  - `checks.py`：图谱校验、冻结指纹、版本比较（纯函数，可独立使用）。
  - `store.py`：SQLite 持久化、版本链、乐观并发发布。
  - `service.py`：课程包比较与受影响未来预约分析。
  - `api.py`：JSON HTTP 接口。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tests/`：契约与课程发布回归测试（26 个用例，含 HTTP 全流程与真实并发线程）。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
