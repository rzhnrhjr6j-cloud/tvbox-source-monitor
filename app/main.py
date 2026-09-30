"""Command line entry point (spec §36).

Subcommands mirror the Makefile targets so the same code path runs locally,
in CI, and by hand:

    discover  check  build  report  pipeline  bootstrap  doctor  node
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import Config, ConfigError, load_config
from .logging_setup import get_logger, setup_logging
from .pipeline import Pipeline
from .storage.sqlite import Store
from .utils.http_client import HttpClient
from .utils.timeutil import to_iso

LOG = get_logger("cli")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help="repository root (defaults to auto-detect)")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument("--log-format", default=None, choices=["json", "text"])
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tvbox-source-monitor",
        description="Automated source discovery, health maintenance and tvbox.json publishing.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_discover = subparsers.add_parser("discover", help="run candidate discovery")
    _add_common(p_discover)

    p_check = subparsers.add_parser("check", help="probe every known source (L1-L5)")
    _add_common(p_check)
    p_check.add_argument("--source", action="append", default=[], help="limit to these source ids")

    p_build = subparsers.add_parser("build", help="score and rebuild tvbox.json")
    _add_common(p_build)

    p_report = subparsers.add_parser("report", help="print a summary of the current state")
    _add_common(p_report)
    p_report.add_argument("--limit", type=int, default=20)

    p_pipeline = subparsers.add_parser("pipeline", help="discover -> health -> build in one run")
    _add_common(p_pipeline)
    p_pipeline.add_argument("--stages", default="discover,health,build",
                            help="comma separated subset of discover,health,build")
    p_pipeline.add_argument("--no-prune", action="store_true", help="skip probe retention pruning")

    p_boot = subparsers.add_parser("bootstrap", help="create missing data files and validate config")
    _add_common(p_boot)

    p_doctor = subparsers.add_parser("doctor", help="check the environment is ready to run")
    _add_common(p_doctor)

    p_node = subparsers.add_parser("node", help="run the probe node (mechanism A/B)")
    p_node.add_argument("--once", action="store_true", help="probe one source and print the JSON result")
    p_node.add_argument("--serve", action="store_true", help="run the HTTP probe API")
    p_node.add_argument("--region", default=None)
    p_node.add_argument("--node-name", default=None)
    p_node.add_argument("--url", default=None, help="source URL for --once")
    p_node.add_argument("--source-id", default="", help="optional source id for --once")
    p_node.add_argument("--host", default="0.0.0.0")
    p_node.add_argument("--port", type=int, default=8080)
    p_node.add_argument("--allow-private", action="store_true",
                        help="permit private addresses (testing only)")

    return parser


def _config_from(args: argparse.Namespace) -> Config:
    cfg = load_config(explicit_root=getattr(args, "root", None))
    level = args.log_level or os.environ.get("TVBOX_LOG_LEVEL") or str(cfg.get("app.log_level", "INFO"))
    fmt = args.log_format or os.environ.get("TVBOX_LOG_FORMAT") or str(cfg.get("app.log_format", "json"))
    setup_logging(level, fmt)
    return cfg


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    _print_human(payload)


def _print_human(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        print(payload)
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            print(f"{key}:")
            print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"{key}: {value}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_discover(cfg: Config, args) -> int:
    from .policy import PolicySet
    from .scoring.scorer import Scorer

    with Pipeline(cfg) as _unused:  # keeps client/store lifecycle uniform
        pass
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    client = HttpClient(cfg.section("http"))
    try:
        from .discovery.aggregator import DiscoveryAggregator

        scorer = Scorer(cfg, store)
        aggregator = DiscoveryAggregator(cfg, client, store, scorer, PolicySet(cfg))
        report = aggregator.run()
    finally:
        client.close()
        store.close()
    _emit(report, args.json)
    return 0


def cmd_check(cfg: Config, args) -> int:
    with Pipeline(cfg) as pipeline:
        if args.source:
            wanted = set(args.source)
            pipeline._checkable_sources = lambda: [  # type: ignore[method-assign]
                source for source in Store.list_sources(pipeline.store) if source.id in wanted
            ]
        report = pipeline.run_health()
    _emit(report, args.json)
    return 0


def cmd_build(cfg: Config, args) -> int:
    with Pipeline(cfg) as pipeline:
        report = pipeline.run_build()
    _emit(report, args.json)
    return 0 if report.get("published") else 1


def cmd_pipeline(cfg: Config, args) -> int:
    stages = [stage.strip() for stage in str(args.stages).split(",") if stage.strip()]
    with Pipeline(cfg) as pipeline:
        report = pipeline.run(stages=stages, prune=not args.no_prune)
    _emit(report, args.json)
    return 0


def cmd_report(cfg: Config, args) -> int:
    store = Store(cfg.path("app.db_path", ensure_parent=True))
    try:
        counts = store.count_by_status()
        dist_dir = cfg.path("app.dist_dir", "dist")
        tvbox = dist_dir / "tvbox.json"
        published = None
        if tvbox.is_file():
            try:
                published = json.loads(tvbox.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                published = None
        payload = {
            "generated_at": to_iso(),
            "database": store.stats(),
            "sources_by_status": counts,
            "regions": store.region_health(days=1),
            "published_items": _count_items(published),
            "last_build": (store.last_build().to_dict() if store.last_build() else None),
            "recent_alerts": [alert.to_dict() for alert in store.list_alerts(limit=args.limit)],
            "top_sources": [
                {
                    "name": source.name,
                    "status": source.status,
                    "tier": source.tier,
                    "score": source.score,
                    "stability": source.stability_score,
                    "availability_7d": store.availability(source.id, 7),
                    "last_success_at": source.last_success_at,
                }
                for source in store.list_sources(limit=args.limit)
            ],
        }
    finally:
        store.close()
    _emit(payload, args.json)
    return 0


def cmd_bootstrap(cfg: Config, args) -> int:
    data_dir = cfg.path("app.data_dir")
    data_dir.mkdir(parents=True, exist_ok=True)
    created = []
    templates = {
        "candidates.json": {"urls": []},
        "whitelist.json": {"urls": [], "hosts": [], "source_ids": [], "url_prefixes": []},
        "blacklist.json": {"urls": [], "hosts": [], "source_ids": [], "url_prefixes": [], "reason": {}},
        "source_aliases.json": {"aliases": {}},
    }
    for name, payload in templates.items():
        target = data_dir / name
        if not target.is_file():
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            created.append(str(target.relative_to(cfg.root)))

    store = Store(cfg.path("app.db_path", ensure_parent=True))
    schema_version = store.schema_version
    store.close()

    cfg.path("app.dist_dir", "dist").mkdir(parents=True, exist_ok=True)
    payload = {
        "root": str(cfg.root),
        "created": created,
        "db_schema_version": schema_version,
        "db_path": cfg.relative(cfg.path("app.db_path")),
        "dist_dir": cfg.relative(cfg.path("app.dist_dir")),
        "output_format": cfg.get("output.format"),
        "next_steps": [
            "1. put the sources you are authorised to use into data/candidates.json",
            "2. make pipeline     # discover -> health -> build",
            "3. make report       # see what happened",
        ],
    }
    _emit(payload, args.json)
    return 0


def cmd_doctor(cfg: Config, args) -> int:
    checks: dict[str, Any] = {}

    checks["python"] = {"version": platform.python_version(), "ok": sys.version_info >= (3, 10)}
    checks["git"] = {"path": shutil.which("git"), "ok": bool(shutil.which("git"))}

    try:
        import requests  # noqa: F401

        checks["requests"] = {"ok": True}
    except ImportError:
        checks["requests"] = {"ok": False, "hint": "pip install -r requirements.txt"}

    try:
        import yaml  # noqa: F401

        checks["pyyaml"] = {"ok": True}
    except ImportError:
        checks["pyyaml"] = {"ok": False, "hint": "pip install -r requirements.txt"}

    checks["gh_token"] = {
        "ok": bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")),
        "blocking": False,
        "hint": "set GH_TOKEN so the GitHub discovery adapter can use the code-search API "
                "(the manual candidate pool works without it)",
    }
    checks["alert_webhook"] = {"ok": bool(os.environ.get("ALERT_WEBHOOK_URL")), "optional": True}
    checks["alert_telegram"] = {
        "ok": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "optional": True,
    }
    checks["alert_github_repo"] = {"ok": bool(os.environ.get("ALERT_GITHUB_REPO")), "optional": True}

    db_path = cfg.path("app.db_path", ensure_parent=False)
    db_ok = True
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        db_ok = False
        checks["db_parent"] = {"ok": False, "error": str(exc)}
    checks["database"] = {"ok": db_ok, "path": cfg.relative(db_path), "exists": db_path.is_file(), "writable": db_ok}

    dist_dir = cfg.path("app.dist_dir", "dist")
    dist_ok = True
    try:
        dist_dir.mkdir(parents=True, exist_ok=True)
        probe_file = dist_dir / ".write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        dist_ok = False
        checks["dist_error"] = str(exc)
    checks["dist"] = {"ok": dist_ok, "path": cfg.relative(dist_dir)}

    enabled = [str(item) for item in (cfg.get("discovery.enabled_adapters") or [])]
    if "github" in enabled and not checks["gh_token"]["ok"]:
        checks["gh_token"]["blocking"] = True
    checks["adapters"] = {
        "ok": True,
        "enabled": enabled,
        "github_active": bool(checks["gh_token"]["ok"]) and "github" in enabled,
    }
    checks["nodes"] = {
        "ok": True,
        "configured": len(cfg.get("regions.nodes") or []),
        "local_region": cfg.get("regions.local.region"),
    }
    checks["output_format"] = {"ok": True, "value": cfg.get("output.format")}
    checks["config"] = {"ok": True, "files": ["app", "regions", "scoring", "discovery", "notifications"]}

    problems, warnings = [], []
    for name, value in checks.items():
        if not isinstance(value, dict) or value.get("ok") is not False:
            continue
        (problems if value.get("blocking", not value.get("optional", False)) else warnings).append(name)
    ok = not problems
    _emit({"ok": ok, "problems": problems, "warnings": warnings, "checks": checks}, args.json)
    return 0 if ok else 1


def cmd_node(cfg: Config, args) -> int:
    node_dir = cfg.root / "node"
    script = node_dir / "probe.py"
    if not script.is_file():
        print(f"probe node not found at {script}", file=sys.stderr)
        return 2
    argv = [sys.executable, str(script)]
    argv.append("--serve" if args.serve else "--once")
    if args.region:
        argv += ["--region", args.region]
    if args.node_name:
        argv += ["--node", args.node_name]
    if args.url:
        argv += ["--url", args.url]
    if args.source_id:
        argv += ["--source-id", args.source_id]
    if args.allow_private:
        argv.append("--allow-private")
    if args.serve:
        argv += ["--host", args.host, "--port", str(args.port)]
    return subprocess.call(argv, cwd=str(cfg.root))


def _count_items(output: Any) -> int:
    if isinstance(output, list):
        return len(output)
    if isinstance(output, dict):
        for key in ("urls", "sites", "list", "lives"):
            value = output.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


COMMANDS = {
    "discover": cmd_discover,
    "check": cmd_check,
    "build": cmd_build,
    "report": cmd_report,
    "pipeline": cmd_pipeline,
    "bootstrap": cmd_bootstrap,
    "doctor": cmd_doctor,
    "node": cmd_node,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "node":
            cfg = load_config(explicit_root=getattr(args, "root", None))
            return cmd_node(cfg, args)
        cfg = _config_from(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    handler = COMMANDS[args.command]
    try:
        return handler(cfg, args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
