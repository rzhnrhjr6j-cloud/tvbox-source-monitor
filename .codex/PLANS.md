# ExecPlans

Use an ExecPlan for complex features, cross-module refactors, migrations, or work expected to span multiple sessions.

Each plan should contain:
1. Objective and non-goals
2. Current state / constraints
3. Requirements and acceptance criteria
4. Design / architecture impact
5. Implementation steps with task IDs
6. Validation strategy and evidence IDs
7. Rollback / recovery plan
8. Exit criteria and handoff

Treat the plan as a living document. Update it when scope, design, dependencies, or evidence changes.

## T-005 - 播放级发布门槛

1. Objective and non-goals
   - Objective: 只发布近期确有搜索和播放证据的源，同时保留用户实机验证过、被明确加入白名单的例外源，减少手机端“能加载但看不了”的源。
   - Non-goals: 不改源发现范围，不声称已经证明所有客户端都能播放，不替代真实大陆节点。
2. Current state / constraints
   - 最近 102 个探测源里，HTTP/配置解析通过 101 个，搜索通过 11 个，播放通过 4 个。
   - 线上发布 86 个源，和真实可播数量明显不匹配。
   - 用户手机实测结果为 T-002 `PARTIAL / REAL_CLIENT`。
3. Requirements and acceptance criteria
   - REQ-104 / AC-104：发布源必须有近期搜索和播放双 100 分证据，播放 Content-Type 必须是允许的媒体类型。
   - REQ-105 / AC-105：用户手机实测确认可播的源可进入人工白名单，绕过自动播放门槛继续发布；检测、评分和镜像仍照常执行。
4. Design / architecture impact
   - 修改 `config/app.yaml` 的 `output.quality_gate`。
   - 修改 `app/build/builder.py::eligible()`，过滤没有近期播放证据的源。
   - `Source.whitelisted=true` 的源在播放门槛前直接放行，但仍需通过镜像准备。
   - `data/whitelist.json` 登记人工保留的 source_id，数据库同步写入 `whitelisted=1`。
   - 首次严格裁剪通过 `bypass_drop_ratio` 绕过旧的跌幅安全阀，但仍保留 `min_sources`。
5. Implementation steps with task IDs
   - T-005：登记变更，修改配置和构建筛选，补白名单例外测试，运行 `make test`，用真实 DB 重建产物。已完成。
6. Validation strategy and evidence IDs
   - 单元/端到端测试覆盖：无播放证据不发布、`text/html` 不算播放、HLS Content-Type 可发布、门槛关闭保持旧行为。
   - 白名单测试覆盖：没有搜索和播放证据时，人工白名单源仍可发布。
   - 证据：E-007 手机实测；E-008 聚焦测试与全量测试；E-009 首次严格构建；E-010 白名单重建。
7. Rollback / recovery plan
   - 将 `output.quality_gate.enabled` 设为 `false` 可恢复旧筛选行为。
   - 发布失败继续保留上一版 `tvbox.json`。
8. Exit criteria and handoff
   - 测试通过，严格入口由真实 DB 生成 4 个源，其中包含人工白名单的“精东（24站）”和“豆了（159站）”，本地退出条件已满足。
   - 待办：提交并发布新产物后，由用户在手机上复测固定入口，确认实际可播性。
