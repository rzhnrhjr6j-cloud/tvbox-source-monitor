# 现场演示（规格 §33）

一条命令跑完全部十项：

```bash
make demo
# 等价于 python scripts/demo.py
```

它会在本机起**真实的** HTTP 点播源（不是 mock），然后驱动真实 pipeline。
全部通过时最后输出 `结果: 10/10 通过`，退出码 0。

想手工戳那个源：

```bash
make serve-demo
```

---

## 十项分别在证什么

| # | 演示 | 实际发生的事 | 看哪里 |
|:--:|:---|:---|:---|
| 1 | 加入一个候选源 | 往 `data/candidates.json` 的 `urls[]` 写地址 | 该文件的 `urls` 长度 |
| 2 | 系统自动发现 | 跑 `discover`：准入流水线 normalize → 可达 → content-type → JSON → schema → 候选分 | `discover.admitted` |
| 3 | 自动判断配置有效 | L1–L5 五层检测，L2 认出 `schema=single` | 源的 `status` / `schema` |
| 4 | 一个地区成功、一个地区失败 | CN 与 JP 各做一次真实探测，源的可达性在两次之间被切换，`decide()` 仲裁 | `global_status == REGIONAL` |
| 5 | Source score 自动变化 | 打坏播放层 → 重算 → 分数下降 | 前后 `score` 差值 |
| 6 | 连续失败后自动从最终 JSON 移除 | 连败到 `fail_pause_output`，被暂停输出，重建后从产物消失 | 产物条数 2 → 1 |
| 7 | 源恢复后自动重新进入候选 | 连击爬满 `recover_to_active` 后回到 ACTIVE，重新进产物 | `status=active`，产物 1 → 2 |
| 8 | GitHub Pages 自动更新 | 校验 `dist/` 三件产物与 `dashboard/` 三件面板文件齐全 | `pages.yml` 的 `workflow_run` 触发链 |
| 9 | 影视仓 6.1.8 固定地址无需修改 | 打印产物顶层/条目字段，确认地址固定 | `https://<user>.github.io/<repo>/tvbox.json` |
| 10 | 一次 build 失败时自动保持旧版 JSON | 所有源下线 → 构建被安全阀拦下 → 产物**字节级不变** | `before/after` 字节相等 |

---

## 演示 4 的诚实说明

「一个地区成功、一个地区失败」在本机无法产生真实的跨境网络差异。
演示的做法是：对同一个源做两次**真实 HTTP 探测**，两次之间把源的可达性切换一次，
然后把两份真实 `ProbeResult` 交给生产用的 `decide()` 仲裁。

也就是说：**仲裁逻辑和探测逻辑都是真的，只有「地区性网络差异」这个原因是模拟的。**
要看真实多地区数据，需要部署自托管 runner 或远程探针节点
（`config/regions.yaml` 的 `nodes[]`，实现见 `node/probe.py`）。

## 演示 8 / 9 的诚实说明

这两项无法离线执行：

- **演示 8** 只能验证产物与面板文件齐全，以及 Pages workflow 的触发链正确；
  真正的发布要在 GitHub 上跑。
- **演示 9** 只能验证发布文件的形状稳定。**字段布局必须用你本机影视仓 6.1.8
  的真实配置核对**（规格 §18 明令不得照抄网络文章）。形状不对时改
  `config/app.yaml` 的 `output.format` 或 `output.template`，不需要改代码。

---

## 手工演示路线（想边讲边操作）

```bash
# 起一个真实的本地源
make serve-demo
# 输出形如：config : http://127.0.0.1:54321/config.json

# 另开一个终端，把它加进候选池
python - <<'EOF'
import json, pathlib
p = pathlib.Path("data/candidates.json")
data = json.loads(p.read_text(encoding="utf-8"))
data["urls"] = ["http://127.0.0.1:54321/config.json"]
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
EOF

make discover        # 演示 1、2
make check           # 演示 3
make build           # 生成 dist/tvbox.json
cat dist/tvbox.json
make report          # 分数、地区、状态

# 演示 6/7：把 serve-demo 那个终端 Ctrl-C 掉，再
make check           # 连做几次，看它被暂停输出
make build
cat dist/tvbox.json  # 源消失了，但旧文件被完整保留
```

演示 10 的手工版：

```bash
md5 dist/tvbox.json
# 把源全部弄挂（Ctrl-C 掉 serve-demo），然后连跑几次 check
make check check check check check
make build
md5 dist/tvbox.json   # 哈希不变；日志里有 blocked by the safety valve
ls dist/backup/       # 每日备份还在
```
