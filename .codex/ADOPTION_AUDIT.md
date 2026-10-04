# Project Audit

Repository: `rzhnrhjr6j-cloud/tvbox-source-monitor`

Running state: GitHub Actions + GitHub Pages 已在线；`main` 由定时任务持续提交 `data/` 和 `dist/`。

Git/tag state: 本地和远端基线为 `062fc3af25fd6c41a0cf2e166942f76b40d09201`；分支 `main`；无 Git tag。

Stable baseline: `062fc3af25fd6c41a0cf2e166942f76b40d09201`。

Product/code/database versions:

| 项目 | 审计值 | 证据 |
|:--|:--|:--|
| Product | `1.0.0` | `pyproject.toml`、`config/app.yaml` |
| Requirement baseline | `R1.0` | 本次接管重建 |
| Code | `v1.0.0` | 当前代码与产品版本一致 |
| Database | `DB-3` | `data/monitor.db` 的 `meta.schema_version=3` |
| Published output | 86 项 | `dist/tvbox.json`、线上 URL |

## Implemented Capabilities

| 能力 | 实现位置 | 审计结论 |
|:--|:--|:--|
| 自动发现与准入 | `app/discovery/`、`app/parsers/` | 已实现，支持 GitHub、Web、手工候选和多仓拆子源 |
| L1-L5 健康检测 | `app/checks/` | 已实现，含 HTTP、配置、搜索、详情和播放探针 |
| 评分与生命周期 | `app/scoring/`、`app/policy.py` | 已实现，含稳定性、衰减、暂停、判死和恢复阶梯 |
| 多地区与 quorum | `app/regions/`、`node/probe.py` | 代码已实现，但线上只有伪 CN 单区，见风险 RISK-001 |
| 构建、镜像和安全阀 | `app/build/` | 已实现，含镜像、内部资源改写、last-known-good 和备份 |
| 告警 | `app/alerts/` | 已实现 GitHub Issues、Webhook、Telegram 三类通道 |
| 存储与迁移 | `app/storage/` | SQLite 当前 schema 3 |
| Dashboard | `dashboard/` | 只读面板已接入 Pages |
| 定时自动化 | `.github/workflows/` | health-check、discover、nightly-build、pages 最近均成功 |
| 安全边界 | `app/utils/ssrf.py` | 有 SSRF 阻断、响应大小限制、超时和重定向上限 |

## Current Runtime Snapshot

| 指标 | 值 |
|:--|:--|
| 线上入口 | `https://rzhnrhjr6j-cloud.github.io/tvbox-source-monitor/tvbox.json` |
| 线上 HTTP | 200 |
| 线上条目 | 86 |
| 产物生成时间 | `2026-10-03T23:21:40Z` |
| active / failed | 100 / 1 |
| primary / backup / experimental | 0 / 0 / 86 |
| 当前地区 | 只有 `CN`，但实际是 GitHub 海外 runner |
| Git SHA in health.json | 空 |

## Requirements Reconstructed

| ID | 状态 | 说明 |
|:--|:--|:--|
| `REQ-001` | ACTIVE | 完成接管审计和控制基线 |
| `REQ-201` | BASELINE | 自动发现和准入公开源 |
| `REQ-202` | BASELINE | 健康检测、评分和生命周期 |
| `REQ-203` | BASELINE | 安全构建和 Pages 发布 |
| `REQ-204` | BASELINE | 自动调度、告警和只读面板 |
| `REQ-101` | PLANNED | 真实影视仓 6.1.8 输出格式验证 |
| `REQ-102` | PLANNED | 建立真实中国大陆探测节点 |
| `REQ-103` | PLANNED | 降低第三方镜像依赖风险 |

## Known Defects

| ID | 严重度 | 缺陷 | 影响 |
|:--|:--|:--|:--|
| `ISSUE-001` | HIGH | `config/regions.yaml` 的 `nodes: []`，唯一 runner 却标成 `CN` | 海外成功不能代表大陆可达 |
| `ISSUE-002` | HIGH | `output.format: multi` 没有用真实影视仓 6.1.8 验证 | 字段布局可能与客户端不兼容 |
| `ISSUE-003` | HIGH | 主链路依赖 `gh-proxy.com`，备用入口依赖 `jsdelivr` | 第三方失效会直接影响客户端加载 |
| `ISSUE-004` | MEDIUM | 线上 86 个源全部是 `experimental` | 没有形成 primary/backup 分层 |

## Scope Drift

- 项目已经超出“只做检测”的最小范围，实际包含镜像、安全阀、Dashboard、告警和多仓拆分。
- 这些能力目前都在仓库内，但没有在接管前形成控制层任务和验收证据。
- 本轮只补审计和控制层，不把新增能力静默扩张为当前任务。

## Version Inconsistencies

| 位置 | 审计前 | 处理 |
|:--|:--|:--|
| `.codex/VERSION_CONTROL.yaml` | `0.1.0 / R0.1 / DB-000` | 已改为 `1.0.0 / R1.0 / DB-3` |
| `.codex/PROJECT_CONTROL.md` | `REPLACE_ME` | 已改为真实基线 |
| Git tag | 无 `1.0.0` tag | 保持 `DEVELOPMENT`，不标 RELEASED |
| `dist/health.json` | `git_sha` 为空 | 已记为可追溯性缺口，后续任务处理 |

## Unverified Assumptions

| ID | 假设 | 状态 |
|:--|:--|:--|
| `ASM-001` | GitHub Actions 工作流和 Secret 当前可用 | VERIFIED |
| `ASM-002` | `gh-proxy.com` 和 `jsdelivr` 对真实大陆客户端稳定可用 | UNVERIFIED |
| `ASM-003` | 现有测试和 demo 覆盖主要本地链路 | PARTIALLY_VERIFIED |

## Missing Acceptance Evidence

- 没有真实影视仓 6.1.8 的实机加载证据。
- 没有真实中国大陆节点的可达性证据。
- 没有第三方代理长期可用性和切换证据。
- 没有正式 release tag 和 release gate 证据。

## Backlog / Future Changes

| CR | 目标版本 | 任务 | 目的 |
|:--|:--|:--|:--|
| `CR-001` | `1.1.0` | `T-002` | 真实客户端验证输出格式 |
| `CR-002` | `1.1.0` | `T-003` | 真实中国大陆探测节点 |
| `CR-003` | `1.1.0` | `T-004` | 第三方镜像依赖降级 |

## Recommended Baseline

- Product baseline: `1.0.0`
- Requirement baseline: `R1.0`
- Code baseline: `v1.0.0`
- Database baseline: `DB-3`
- Stable commit: `062fc3af25fd6c41a0cf2e166942f76b40d09201`
- Release state: `DEVELOPMENT`

推荐基线不是“已发布版本”，而是“已接管、可继续开发和审计的稳定起点”。

## Human Decisions

| ID | 决定 |
|:--|:--|
| `DEC-001` | 采用 legacy baseline，提交 `062fc3a` |
| `DEC-002` | 因无 tag 和未验证客户端格式，不标 RELEASED |
| `DEC-003` | 本轮不修改业务代码和发布产物 |
| `DEC-004` | 下一任务优先做影视仓 6.1.8 输出格式验证 |

## Recommended Next Task

`T-002`：用真实影视仓 6.1.8 加载固定地址，核对最终 `tvbox.json` 字段布局；若失败，只调整 `output.format` 或 `output.template`，并补回证据。
