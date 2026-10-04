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
   - CR-006：根据手机实测反馈，把 360 和茅台加入黑名单，发布下限调整为 2，只保留精东和豆了。已完成。
6. Validation strategy and evidence IDs
   - 单元/端到端测试覆盖：无播放证据不发布、`text/html` 不算播放、HLS Content-Type 可发布、门槛关闭保持旧行为。
   - 白名单测试覆盖：没有搜索和播放证据时，人工白名单源仍可发布。
   - 证据：E-007 手机实测；E-008 聚焦测试与全量测试；E-009 首次严格构建；E-010 白名单重建；E-011 失败源撤下与双源重建。
7. Rollback / recovery plan
   - 将 `output.quality_gate.enabled` 设为 `false` 可恢复旧筛选行为。
   - 发布失败继续保留上一版 `tvbox.json`。
8. Exit criteria and handoff
   - 测试通过，严格入口由真实 DB 生成 2 个源：“精东（24站）”和“豆了（159站）”。
   - 待办：提交并发布新产物后，由用户在手机上复测固定入口，确认这两个源仍可播。

## T-006 - Android 真机门禁补齐

1. Objective and non-goals
   - Objective: JAR/csp 源必须先在 Android 真机上完成 DexClassLoader 并真实拉到媒体字节，才允许发布；同时修复内容去重只比较 CMS 主机、会误砍 csp 源的问题。
   - Non-goals: 不放开自动播放门槛凑数量，不伪造真机证据，不保证所有手机/网络环境都能播放。
2. Current state / constraints
   - L1-L5 只能证明 HTTP 配置可下载，无法解释手机端 `jar解析失败`。
   - 设备：`emulator-5554`，Android 7.1 API25 arm64。
   - 真机播放证据写入 `data/android_verified.json`，CI 无模拟器但会读取已提交证据。
3. Requirements and acceptance criteria
   - REQ-601 / AC-601：`require_android_jar=true` 时，带 `csp_*`/jar 站点且无新鲜真机可播证据的源不发布。
   - REQ-602 / AC-602：JAR 源的真机可播证据可替代 runner 侧失败的 L5 播放探针，但搜索证据仍必须单独成立。
   - REQ-603 / AC-603：内容去重指纹包含 `csp_*` 类名，内容不同的 JAR 源不再因 CMS 主机子集被误判为镜像。
   - REQ-604 / AC-604：配置未下载成功（`site_count=0`）的真机结果不写入证据文件。
4. Design / architecture impact
   - `app/build/builder.py`：`_android_substitutes_playback`、`ContentProfile.csp_keys`、`_domain_fingerprint`。
   - `config/app.yaml`：`require_android_jar=true`，新增 `android_satisfies_playback=true`。
   - `tools/android_verify.py`：证据记录用原始源 URL，跳过零站点结果。
5. Implementation steps with task IDs
   - T-006：接入 L6 门禁、真机替代规则和 csp 去重修复。已完成。
   - CR-007：清理 `android_verified.json` 的零记录，补测精东并记录真实媒体首字节。已完成。
6. Validation strategy and evidence IDs
   - `tests/test_android_gate.py` 12 项通过；全量测试 213 项通过。
   - 证据：E-012 精东真机播放（`csp_XMVideo`，媒体 200 + `application/vnd.apple.mpegurl`）；E-013 AndroidSoftwares box 真机播放（`动漫巴士`）；E-014 L6 构建由 2 源变 3 源。
7. Rollback / recovery plan
   - `require_android_jar=false` 可恢复只看 L1-L5；`android_satisfies_playback=false` 可关闭真机替代。
   - `content_overlap_threshold=0` 可临时关闭内容去重。
8. Exit criteria and handoff
   - 本地 `make build` 发布 3 个源：“精东（24站）”“豆瓣（39站）”“豆了（159站）”。
   - 待办：提交推送并触发线上工作流后，核对线上 `tvbox.json` 三项均可下载；由用户手机复测三个源。
