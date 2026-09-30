# tvbox-source-monitor

影视仓 6.1.8 / TVBox 配置的**自动源发现与健康维护系统**，在 GitHub Actions 上运行，
产物通过 GitHub Pages 发布。

影视仓里只填一次地址，之后不用再改：

```text
https://<你的用户名>.github.io/<你的仓库名>/tvbox.json
```

之后系统自己跑完这条链路：

```text
发现新源 → 准入校验 → 多地区健康检测 → 评分 → 淘汰 / 恢复
        → 生成 tvbox.json → 安全阀校验 → Pages 自动更新
```

## 这个系统做什么

| 能力 | 说明 |
|:---|:---|
| 自动发现 | GitHub 代码/仓库搜索、手工候选池、多仓配置自动拆子源 |
| 准入校验 | normalize → 可达性 → content-type → JSON 解析 → schema 识别 → 候选评分 |
| 五层健康检测 | L1 HTTP → L2 配置 → L3 搜索 → L4 详情 → L5 播放探针 |
| 多地区 | 本 runner + 自托管节点 / 远程 Probe API，配额仲裁（quorum） |
| 评分与稳定性 | 7 日可用率、30 日/7 日/近期稳定性、延迟、地区一致性、新鲜度衰减 |
| 生命周期 | 单次失败不动 → 5 次暂停输出 → 12 次判死 → 恢复需爬完楼梯 |
| 安全阀 | 构建不合法时**保持旧版 JSON 字节不变**，并留 `last-known-good` + 每日备份 |
| 告警 | GitHub Issues / Webhook / Telegram，同一问题 24h 内去重 |
| 面板 | GitHub Pages 上的只读 Dashboard |

## 本地快速开始

```bash
make install     # 建 .venv 并装依赖
make doctor      # 检查环境与配置
make demo        # 跑完 spec §33 的十个验收演示（真实本地 HTTP 服务）
make pipeline    # discover -> health -> build 全链路
make test        # 测试套件
make report      # 打印当前状态
```

`make demo` 是理解这个系统最快的方式：它会在本机起真实的点播源，然后逐条演示
「加入候选 → 自动发现 → 判定有效 → 一个地区成功一个失败 → 分数变化 →
连续失败被移除 → 恢复后回归 → Pages 产物 → 固定地址 → 构建失败保持旧版」。

手工加源：编辑 `data/candidates.json` 的 `urls[]`，或者直接 `git commit` 它。

## 部署

一键脚本（需要已 `gh auth login`）：

```bash
./deploy.sh --repo tvbox-source-monitor --public
```

脚本会建仓库、推代码、设置 Secrets、开 Pages。详见 `docs/部署指南.md`。

## 配置

所有阈值都在 `config/*.yaml`，代码里没有硬编码阈值。

| 文件 | 内容 |
|:---|:---|
| `config/app.yaml` | 运行参数、HTTP 超时/并发、L1-L5 开关、**输出格式**、安全阀 |
| `config/scoring.yaml` | 评分权重、状态阈值、稳定性窗口、生命周期计数、准入权重 |
| `config/discovery.yaml` | 适配器开关、GitHub 搜索词、准入规则、去重规则 |
| `config/regions.yaml` | 本机地区、远程节点、quorum 比例 |
| `config/notifications.yaml` | 告警开关、抑制窗口、各通道 |

环境变量可以用 `${VAR}` / `${VAR:-default}` 写进 YAML。

## ⚠️ 输出格式必须先核对

`config/app.yaml` 的 `output.format` 默认是 `multi`：

```json
{
  "version": "1.0",
  "generated_at": "2026-09-30T06:00:00Z",
  "urls": [{ "name": "Source-A", "url": "https://example.com/a.json" }]
}
```

规格 §18 明确要求**用真实影视仓 6.1.8 的配置核对字段布局，不得照抄网络文章**。
所以这里没有写死：`output.format` 可选 `multi` / `single` / `sites`，
必要时用 `output.template` 精确覆盖形状（不需要改代码）。写法见 `docs/配置说明.md`。

## 合规边界

只处理公开可访问、你有权使用、明确授权的接口。不破解 DRM、不绕过鉴权或付费墙、
不盗用 Token、不端口扫描、不探测未授权内网目标。探针内置 SSRF 防护（含云元数据
地址与 RFC1918 网段）。

## 目录

```text
app/        业务代码（discovery / parsers / checks / regions / scoring / build / alerts / storage）
config/     全部阈值
data/       候选池、黑白名单、别名、SQLite 数据库
dist/       发布产物 tvbox.json / health.json / dashboard.json + 备份
dashboard/  只读面板
node/       独立探针节点（--once / --serve）
scripts/    CLI 薄封装 + demo 驱动
tests/      测试（真实 HTTP + 真实 SQLite，最终链路不用 mock）
docs/       配置说明 / 故障排查 / 数据库结构 / 部署指南 / 演示
```

更多：`docs/配置说明.md`、`docs/故障排查.md`、`docs/数据库结构.md`、`docs/部署指南.md`。
