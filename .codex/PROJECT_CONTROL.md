# PROJECT CONTROL

详细事实以 `.codex/state/*.yaml` 为准，本文件是人类可读总览。

- Project ID: PROJECT-001
- Project Name: tvbox-source-monitor
- Mission: 用自动化流水线持续发现、校验、体检、评分并发布公开可访问的 TVBox/影视仓源，并在构建或源池异常时安全回退。
- Product Version: 1.1.0
- Requirement Baseline: R1.0
- Code Version: v1.0.0
- Database Version: DB-3
- Phase: P1
- Current Task: T-007
- Stable Baseline: 062fc3af25fd6c41a0cf2e166942f76b40d09201
- Development Baseline: 062fc3af25fd6c41a0cf2e166942f76b40d09201+control
- WIP Limit: 2

## Baseline Notes

- 当前产品版本代码仍在 `main` 上由自动化提交 `data/` 与 `dist/`。
- Git 没有对应 `1.0.0` 的 tag，因此该提交只记为稳定基线，不标记为 RELEASED。
- 接管审计报告见 `.codex/ADOPTION_AUDIT.md`。
- T-002 已由真实手机测试证明“入口可加载”，但旧输出大量源不可播，因此按 `PARTIAL` 处理。
- T-005 已把发布门槛从“配置解析成功”提升为“搜索和真实播放都通过”；用户实机验证过的“精东（24站）”和“豆了（159站）”通过人工白名单保留。手机实测 jar 解析失败的“360”和“茅台”已加入黑名单，发布下限调整为 2。
- T-006 补齐 Android 真机 JAR 门禁：`require_android_jar=true`，无新鲜真机可播证据的 JAR/csp 源不发布；真机播放证据可替代 runner 侧失败的 L5 播放探针。内容去重指纹加入 `csp_*` 类名，修复把 AndroidSoftwares box 误判为“豆了”镜像的问题。精东和 AndroidSoftwares box 均已记录真实媒体首字节证据。
- T-007 修复发布入口、真机证据合并和镜像误杀。`build` 必须显式带 `GITHUB_REPOSITORY`，否则 `dist/tvbox.json` 会指向手机打不开的 `raw.githubusercontent.com` 原点；`jar_check.write_evidence` 只允许不弱于既有记录的真机结果覆盖；`app/build/mirror.py` 把「暂时取不到」和「已证实消失」拆开，超时、5xx、连接被拒和预算耗尽不再删源，只有 404/410、2xx 空 body、或字节证明不是 jar/配置才删除。`豆了（159站）`（`745561a3739c9b0c`）因此稳定回归。
- T-007 最新重建：`build_id=20261005-043906`，数据库 active 331 / degraded 136 / failed 2，真机证据 120 条，新增 1、恢复 0、移除 0；最终发布 49 条，`validation.ok=true`。`精东（24站）`和`豆了（159站）`均在顶层入口，手机实测 jar 解析失败的`360`和`茅台`未混入；`dist/tvbox.json` 的 49 条 URL 全部走 `gh-proxy.com`。待提交推送、线上核对和手机复测；继续用 GitHub `gh search repos` / `gh search code` 找更多候选并补真机可播证据。

## Hard Rule

不能靠聊天里的“完成了”改变项目状态；状态必须满足 machine-checkable 条件。
