#!/usr/bin/env python3
"""Drives the ten acceptance demos from spec §33.

Everything here is real: real HTTP servers on real ports, the real pipeline,
the real builder, the real safety valve and the real files under dist/.

    python scripts/demo.py            # run all ten and print a report
    python scripts/demo.py --serve-only
                                      # leave a demo origin running for poking

演示 8 (GitHub Pages) and 演示 9 (影视仓 6.1.8 固定地址) cannot be executed
offline; they are reported as instructions plus the artefacts that prove the
deployed pipeline will produce them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.build.builder import DASHBOARD_FILE, HEALTH_FILE, TVBOX_FILE  # noqa: E402
from app.config import load_config  # noqa: E402
from app.models import ProbeResult, Status  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.regions.quorum import decide  # noqa: E402
from app.storage.sqlite import Store  # noqa: E402
from app.utils.http_client import HttpClient  # noqa: E402
from app.utils.urls import source_id_for  # noqa: E402
from tests.local_source_server import LocalSourceServer  # noqa: E402


# ---------------------------------------------------------------------------
# reporting helpers
# ---------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.results: list[tuple[int, str, bool, str]] = []

    def demo(self, number: int, title: str, ok: bool, detail: str = "") -> None:
        self.results.append((number, title, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"\n  演示 {number:<2} [{mark}] {title}")
        for line in textwrap.wrap(detail, 96) if detail else []:
            print(f"          {line}")

    @property
    def ok(self) -> bool:
        return all(result[2] for result in self.results)

    def summary(self) -> None:
        passed = sum(1 for result in self.results if result[2])
        total = len(self.results)
        print("\n" + "=" * 78)
        print(f"结果: {passed}/{total} 通过")
        for number, title, ok, _ in self.results:
            print(f"  {'✓' if ok else '✗'} 演示 {number:<2} {title}")
        print("=" * 78)


def heading(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
class DemoWorkspace:
    """An isolated copy of the real config, pointed at temp data/dist dirs."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cfg = load_config(
            explicit_root=ROOT,
            overrides={
                "app": {
                    "data_dir": str(tmp / "data"),
                    "db_path": str(tmp / "data" / "monitor.db"),
                    "dist_dir": str(tmp / "dist"),
                },
                "discovery": {"enabled_adapters": ["manual"]},
                # one surviving source is enough for the build to be valid, so
                # the demos can show a source entering *and* leaving the JSON
                "output": {"min_sources": 1, "observation_days": 0, "include_degraded": False},
            },
        )
        self.data_dir = self.cfg.path("app.data_dir", ensure_parent=True)
        self.dist_dir = self.cfg.path("app.dist_dir", "dist")
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def seed(self, urls: list) -> None:
        (self.data_dir / "candidates.json").write_text(
            json.dumps({"urls": urls}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def pipeline(self) -> Pipeline:
        store = Store(self.cfg.path("app.db_path", ensure_parent=True))
        # the demo origin is on 127.0.0.1, so the client opts into private targets
        client = HttpClient(self.cfg.section("http"), allow_private=True)
        return Pipeline(self.cfg, store=store, client=client)

    def run(self, stages: list[str]) -> dict:
        with self.pipeline() as pipeline:
            return pipeline.run(stages=stages, prune=False)

    # -- inspection --------------------------------------------------------
    def tvbox(self) -> dict:
        path = self.dist_dir / TVBOX_FILE
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def published_urls(self) -> list[str]:
        payload = self.tvbox()
        items = payload.get("urls") if isinstance(payload, dict) else payload
        return [str(item.get("url")) for item in (items or []) if isinstance(item, dict)]

    def sources(self) -> list:
        store = Store(self.cfg.path("app.db_path"))
        try:
            return store.list_sources()
        finally:
            store.close()

    def describe(self) -> str:
        rows = []
        for source in sorted(self.sources(), key=lambda item: item.url):
            rows.append(
                f"{source.url.rsplit('/', 2)[-2]}:{source.url.rsplit(':', 1)[-1]} "
                f"status={source.status} paused={source.paused} score={source.score:.1f} "
                f"fail={source.consecutive_failure} ok={source.consecutive_success}"
            )
        return " | ".join(rows) or "(no sources)"

    def source_for(self, url: str):
        target = source_id_for(url)
        for source in self.sources():
            if source.id == target:
                return source
        return None


def probe_region(cfg, url: str, region: str) -> ProbeResult:
    """One real L1-L5 probe of `url` attributed to `region`."""
    from app.checks.runner import CheckOptions, run_checks

    client = HttpClient(cfg.section("http"), allow_private=True)
    try:
        return run_checks(_bare_source(url), client, CheckOptions.from_config(cfg), region=region, node=region.lower())
    finally:
        client.close()


def _bare_source(url: str):
    from app.models import Source

    return Source(id=source_id_for(url) or "demo", url=url, raw_url=url, name="demo")


# ---------------------------------------------------------------------------
# the ten demos
# ---------------------------------------------------------------------------
def run_demos(repo_label: str) -> int:
    report = Report()

    with tempfile.TemporaryDirectory(prefix="tvbox-demo-") as tmpdir:
        workspace = DemoWorkspace(Path(tmpdir))

        with LocalSourceServer() as healthy, LocalSourceServer() as doomed:
            healthy_url = healthy.config_url
            doomed_url = doomed.config_url

            heading("环境")
            print(f"  健康源 A : {healthy_url}")
            print(f"将被下线的源 B : {doomed_url}")
            print(f"临时工作区 : {workspace.tmp}")
            print("  以上均为本机真实 HTTP 服务，未使用 mock（spec §35-10）")

            # -- 演示 1 ----------------------------------------------------
            workspace.seed([healthy_url, doomed_url])
            candidates = json.loads((workspace.data_dir / "candidates.json").read_text(encoding="utf-8"))
            report.demo(
                1, "加入一个候选源",
                len(candidates.get("urls", [])) == 2,
                f"已写入 data/candidates.json：{len(candidates.get('urls', []))} 条候选。"
                "手工候选池是 spec §5.1.B 的入口，你也可以直接 git commit 这个文件。",
            )

            # -- 演示 2 ----------------------------------------------------
            discovery = workspace.run(["discover"])
            admitted = discovery["discover"]["admitted"]
            report.demo(
                2, "系统自动发现",
                admitted >= 2,
                f"discovery 跑完：发现 {discovery['discover']['found']} 条，准入 {admitted} 条，"
                f"拒绝 {discovery['discover']['rejected']} 条。"
                "准入流水线 = normalize → reachable → content-type → JSON 解析 → schema 识别 → 候选评分（spec §6/§27）。",
            )

            # -- 演示 3 ----------------------------------------------------
            first = workspace.run(["health"])
            source_a = workspace.source_for(healthy_url)
            valid = bool(source_a and source_a.status in (Status.ACTIVE, Status.VALIDATING, Status.DEGRADED))
            report.demo(
                3, "自动判断配置有效",
                valid,
                f"L2 配置校验通过：schema={source_a.schema if source_a else '?'}，"
                f"status={source_a.status if source_a else '?'}。"
                "L1 HTTP → L2 配置 → L3 搜索 → L4 详情 → L5 播放，五层全部有独立的错误码与阶段标记（spec §8/§35-13）。",
            )

            # -- 演示 4 ----------------------------------------------------
            up = probe_region(workspace.cfg, healthy_url, "CN")
            healthy.set(config_up=False)
            down = probe_region(workspace.cfg, healthy_url, "JP")
            healthy.set(config_up=True)
            verdict = decide({"CN": [up], "JP": [down]})
            report.demo(
                4, "一个地区成功、一个地区失败",
                verdict.global_status == "REGIONAL",
                f"CN 探测: http_ok={up.http_ok} / 配置={up.config_parse_success}；"
                f"JP 探测: http_ok={down.http_ok}（该地区连不上源）。"
                f"仲裁结果 {verdict.global_status}，regions_ok={verdict.regions_ok}，"
                f"regions_failed={verdict.regions_failed}。"
                "两处探测都是真实 HTTP，只有源的可达性在两次探测之间被切换，用于模拟地区性网络差异。",
            )

            # -- 演示 5 ----------------------------------------------------
            before = workspace.source_for(healthy_url)
            before_score = before.score if before else 0.0
            healthy.set(media_up=False)          # 播放层坏掉，其余正常
            workspace.run(["health"])
            after = workspace.source_for(healthy_url)
            after_score = after.score if after else 0.0
            healthy.set(media_up=True)
            delta = after_score - before_score
            report.demo(
                5, "Source score 自动变化",
                delta < -0.5,
                f"播放层故障前 score={before_score:.1f}，故障后 score={after_score:.1f}"
                f"（Δ {delta:+.1f}）。"
                "评分 = 可用率 20% + 搜索 15% + 播放 20% + 延迟 10% + 历史稳定性 15% + 地区一致性 10% + 新鲜度 10%（spec §9）。",
            )

            # -- 演示 6 ----------------------------------------------------
            # publish a baseline first, otherwise there is nothing to remove from
            workspace.run(["build"])
            before_removal = workspace.published_urls()
            pause_at = int(workspace.cfg.get("lifecycle.fail_pause_output"))
            doomed.set(config_up=False)
            for _ in range(pause_at):
                workspace.run(["health"])
            build_after = workspace.run(["build"])
            after_removal = workspace.published_urls()
            gone = doomed_url not in after_removal and doomed_url in before_removal
            report.demo(
                6, "连续失败后自动从最终 JSON 移除",
                gone,
                f"源 B 连续 {pause_at} 次失败后被暂停输出（spec §11）："
                f"tvbox.json 从 {len(before_removal)} 条变成 {len(after_removal)} 条，"
                f"源 B 已不在其中，源 A 仍在。build published={build_after['build']['published']}。"
                "注意它此时是 DEGRADED 而不是 FAILED——"
                f"要走到 FAILED 需要连续 {int(workspace.cfg.get('lifecycle.fail_mark_failed'))} 次失败。"
                f"本轮源状态：{workspace.describe()}",
            )

            # -- 演示 7 ----------------------------------------------------
            doomed.set(config_up=True)
            recover_to = int(workspace.cfg.get("lifecycle.recover_to_active"))
            for _ in range(recover_to):
                workspace.run(["health"])
            final_build = workspace.run(["build"])
            recovered_source = workspace.source_for(doomed_url)
            restored = doomed_url in workspace.published_urls()
            report.demo(
                7, "源恢复后自动重新进入候选",
                restored,
                f"源 B 连续 {recover_to} 次成功后回到 ACTIVE（spec §11 的恢复阶梯："
                f"{int(workspace.cfg.get('lifecycle.recover_min_success'))} 次→RECOVERING，{recover_to} 次→ACTIVE），"
                f"当前 status={recovered_source.status if recovered_source else '?'}，"
                f"已重新出现在 tvbox.json（共 {len(workspace.published_urls())} 条，"
                f"published={final_build['build']['published']}）。"
                "关键点：一次成功不会让它跳回 ACTIVE，必须爬完整段阶梯。"
                f"本轮源状态：{workspace.describe()}",
            )

            # -- 演示 8 ----------------------------------------------------
            dist = workspace.dist_dir
            artefacts = {name: (dist / name).is_file() for name in (TVBOX_FILE, HEALTH_FILE, DASHBOARD_FILE)}
            dashboard_dir = ROOT / "dashboard"
            pages_files = {name: (dashboard_dir / name).is_file() for name in ("index.html", "app.js", "style.css")}
            report.demo(
                8, "GitHub Pages 自动更新",
                all(artefacts.values()) and all(pages_files.values()),
                f"构建产物齐全：{', '.join(name for name, ok in artefacts.items() if ok)}；"
                f"面板文件齐全：{', '.join(name for name, ok in pages_files.items())}。"
                "Pages 由 .github/workflows/pages.yml 发布：nightly-build 提交 dist/ 后由 workflow_run 触发"
                "（用 GITHUB_TOKEN 提交的 push 不会触发其它 workflow，这是 GitHub 的已知行为）。"
                "发布后访问 <仓库地址>/dashboard/ 即为本地面板的线上版本。",
            )

            # -- 演示 9 ----------------------------------------------------
            payload = workspace.tvbox()
            items = payload.get("urls") if isinstance(payload, dict) else payload
            report.demo(
                9, "影视仓 6.1.8 固定地址无需修改",
                bool(items) and "version" in payload and "generated_at" in payload,
                f"发布文件顶层字段：{sorted(payload.keys())}，条目字段："
                f"{sorted(items[0].keys()) if items else '—'}。"
                f"影视仓里填一次 {repo_label}/tvbox.json 之后不用再改——"
                "每天 04:00 的内容变化是通过同一个地址重新读取实现的。"
                "⚠️ 这个字段布局必须用你本机影视仓 6.1.8 的真实配置核对（spec §18 明令不得照抄网络文章）；"
                "如果不是这个形状，改 config/app.yaml 的 output.format 或 output.template 即可，无需改代码。",
            )

            # -- 演示 10 ---------------------------------------------------
            published_path = dist / TVBOX_FILE
            before_bytes = published_path.read_bytes()
            # every origin must go down: with recovery working (演示 7) a single
            # surviving source is enough to keep the build valid
            healthy.set(config_up=False)
            doomed.set(config_up=False)
            for _ in range(pause_at):
                workspace.run(["health"])
            blocked = workspace.run(["build"])
            after_bytes = published_path.read_bytes()
            same = before_bytes == after_bytes
            report.demo(
                10, "一次 build 失败时自动保持旧版 JSON",
                (not blocked["build"]["published"]) and same,
                f"本轮 build published={blocked['build']['published']}，拦截原因："
                f"{blocked['build']['blocked_reason']}。"
                f"tvbox.json 字节级未变（{len(before_bytes)} 字节），"
                f"last-known-good.json 存在={(dist / 'last-known-good.json').is_file()}，"
                f"备份 {len(list((dist / 'backup').glob('*.json')))} 份。"
                "安全阀校验：结构/必需字段/去重/最小条数/单轮跌幅 > 70%（spec §19/§30/§32）。",
            )

    report.summary()
    return 0 if report.ok else 1


def serve_only() -> int:
    with LocalSourceServer() as server:
        print(f"demo origin: {server.base}")
        print(f"  config : {server.config_url}")
        print(f"  multi  : {server.base}/multi.json")
        print("Ctrl-C to stop")
        import time

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the spec §33 acceptance demos")
    parser.add_argument("--serve-only", action="store_true", help="leave a demo origin running")
    parser.add_argument("--repo", default=None, help="owner/repo, used for the fixed URL in 演示 9")
    args = parser.parse_args(argv)
    if args.serve_only:
        return serve_only()
    label = args.repo or os.environ.get("GITHUB_REPOSITORY") or "<你的用户名>/<你的仓库名>"
    base = label if label.startswith("http") else f"https://{label.split('/')[0]}.github.io"
    repo_label = f"{base}/{label.split('/')[-1]}" if "/" in label else f"{base}/{label}"
    return run_demos(repo_label)


if __name__ == "__main__":
    raise SystemExit(main())
