# 主题课程依赖发布

研学课程由讲解主题、实践环节与安全说明组成，节点之间存在前置依赖，并各自携带
适用年龄、必备材料与授权期限。本项目维护课程版本与依赖图谱：内容负责人更新前置
主题后，已发布课程包保持冻结不受影响；每次发布、紧急替代、局部停用与课程复制都
形成明确的版本链；接口可比较任意两个课程包，并列出受影响的未来预约。

## 设计要点

- **草稿与发布分离**：草稿可随时更新（`course_series`），发布时对完整图谱做快照
  冻结（`packages.snapshot`），已发布包永不被原地修改。
- **发布前校验**（`graph.validate_course`）：
  - 循环依赖（DFS 三色标记，返回完整环路）
  - 缺失条件（依赖指向不存在的节点；实践环节必须有前置主题）
  - 适用年龄冲突（节点年龄区间须与课程目标年龄有交集）
  - 必备材料缺口（对照课程已备材料清单）
  - 授权期限（发布时点有效；声明开课窗口时须覆盖窗口末端）
- **明确版本链**：每个课程包带 `version / parent_id / root_package_id / operation`。
  - `publish`：新版本指向前一版本
  - `emergency_replace`：替换节点后重新校验并冻结
  - `suspend_nodes`：在快照上标记停用，停用不得留下悬空前置
  - `copy`：新课程 v1 的 `parent_id` 指向源课程包，沿用同一谱系根
- **并发发布**：写操作在 `BEGIN IMMEDIATE` 事务内重读最新版本号；携带
  `expected_version` 的乐观锁发布冲突时返回 `409 version_conflict`，不带基线的
  并发发布被事务串行化，相同内容幂等不重复冻结。
- **预约影响**：预约绑定具体课程包；查询沿 `root_package_id` 找到谱系内各课程的
  最新版本，列出仍预约在旧包上的、未取消的未来预约（含复制分支）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/curriculum_service/`：课程依赖发布后端（仅标准库）。
  - `graph.py`：图谱规范化、五类发布前校验、内容指纹
  - `store.py`：SQLite 存储、版本链、写事务
  - `service.py`：发布/紧急替代/局部停用/复制/比较/预约影响
  - `server.py`：HTTP JSON API
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与服务回归测试。

## 运行

```bash
PYTHONPATH=src python3 -m curriculum_service --db data/curriculum.db --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/courses` | 保存/更新课程草稿 |
| GET | `/courses` / `/courses/{code}` | 系列列表 / 草稿详情 |
| POST | `/courses/{code}/validate` | 只校验不发布（`?as_of=YYYY-MM-DD`） |
| POST | `/courses/{code}/publish` | 校验通过后冻结发布（`?expected_version=n`） |
| GET | `/courses/{code}/packages` | 该课程全部已发布版本 |
| GET | `/packages/{id}` | 课程包完整冻结快照 |
| GET | `/packages/{id}/chain` | 沿 parent 回溯的版本链 |
| GET | `/packages/{id}/compare?with={id}` | 比较两个课程包（关系、节点/材料/年龄/停用差异） |
| POST | `/courses/{code}/emergency-replace` | 紧急替代节点并发布（body：`node_id`、`replacement`、`reason`） |
| POST | `/courses/{code}/suspensions` | 局部停用节点并发布（body：`node_ids`、`reason`） |
| POST | `/courses/{code}/copy` | 复制为新课程（body：`new_code`、`title`） |
| POST | `/reservations` | 创建预约（绑定 `package_id`） |
| POST | `/reservations/{id}/cancel` | 取消预约 |
| GET | `/packages/{id}/affected-reservations` | 受该谱系更新影响的未来预约 |

课程节点示例：

```json
{
  "id": "P1", "type": "practice", "title": "水质检测",
  "age_min": 8, "age_max": 12, "depends_on": ["T1"],
  "required_materials": ["安全帽", "标本盒"],
  "license": {"licensed_to": "湿地馆",
              "valid_from": "2026-01-01", "valid_until": "2028-01-01"}
}
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
