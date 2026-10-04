#!/usr/bin/env python3
"""Drive the Android crawler verifier and collect per-site verdicts.

The pipeline runs on a Linux runner and can only see HTTP.  A TVBox client
also has to DexClassLoader every ``csp_*`` jar and run it on Android - which is
exactly the step that produced the "jar解析失败" reports this project keeps
getting.  This script drives ``tools/android-verifier`` on a connected device
or emulator so a config can be judged the same way the client judges it.

Usage::

    python3 tools/android_verify.py --config-url https://.../dist/sources/x.json
    python3 tools/android_verify.py --config-file dist/sources/x.json --out /tmp/v.json
    python3 tools/android_verify.py --config-url https://.../x.json --isolate-sites

Local files are served to the device over ``adb reverse`` so candidates that
have not been published yet can be verified too.  Exit code is 0 when at least
one site in every config reached a media URL, 1 otherwise.

``--isolate-sites`` runs one instrumentation per ``csp_*`` / jar site.  A
broken native library can SIGABRT the verifier process before it writes a
verdict; isolating each site keeps that crash from hiding every later site in
the same config.

``--record`` merges the verdicts into ``data/android_verified.json`` so the
builder's L6 Android gate can consult them.  The file is keyed by
``source_id`` (sha256 of the normalised source URL); when a config is served
from a local mirror the real identity must be passed with ``--source-url``,
paired by position with the configs.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checks.jar_check import AndroidRecord, write_evidence  # noqa: E402
from app.utils.timeutil import to_iso  # noqa: E402
from app.utils.urls import source_id_for  # noqa: E402
from app.utils.http_client import HttpClient  # noqa: E402

DEFAULT_SERIAL = "emulator-5554"
TEST_PACKAGE = "com.tvbox.verifier.test"
RUNNER = "androidx.test.runner.AndroidJUnitRunner"
DEVICE_RESULT = "/sdcard/Android/data/com.tvbox.verifier/files/verify-result.json"
DEFAULT_RECORD = Path(__file__).resolve().parents[1] / "data" / "android_verified.json"


def adb_path() -> str:
    """Prefer the SDK's adb; fall back to whatever is on PATH."""
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        candidate = Path(sdk) / "platform-tools" / "adb"
        if candidate.exists():
            return str(candidate)
    return shutil.which("adb") or "adb"


def adb(
    serial: str,
    *args: str,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    command = [adb_path()]
    if serial:
        command += ["-s", serial]
    command += list(args)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: D102 - silence access logs
        pass


class LocalOrigin:
    """Serve a directory over loopback so the device can pull local files."""

    def __init__(self, root: Path):
        handler = functools.partial(_QuietHandler, directory=str(root))
        self.server = _Server(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.root = root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "LocalOrigin":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def url_for(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.root.resolve())
        return f"http://127.0.0.1:{self.port}/{relative.as_posix()}"


def is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def strip_md5(value: str) -> str:
    """Drop the optional ``;md5`` suffix TVBox uses for jar cache busting."""
    return value.split(";", 1)[0]


def is_spider_site(api: object) -> bool:
    """Match the Android verifier's definition of a jar/CMS spider site."""
    text = str(api or "")
    if not text or text.startswith("http"):
        return False
    if text.startswith("csp_") or text.startswith("Csp_"):
        return True
    return "." not in text


def spider_site_keys(config: Mapping[str, Any]) -> list[str]:
    """Return unique site keys in the same order Android visits them.

    An empty string marks a spider site with no key.  Those cannot be selected
    with ``siteKey``; callers must record them as unverified instead of
    silently dropping them.
    """
    sites = config.get("sites") if isinstance(config, Mapping) else None
    if not isinstance(sites, list):
        return []
    keys: list[str] = []
    for item in sites:
        if not isinstance(item, Mapping) or not is_spider_site(item.get("api")):
            continue
        key = str(item.get("key") or "")
        if key not in keys:
            keys.append(key)
    return keys


def load_config_document(
    target: Mapping[str, Any],
    *,
    connect_timeout_ms: int,
    read_timeout_ms: int,
) -> tuple[dict | None, str]:
    """Load a candidate config on the host for site enumeration."""
    local_path = target.get("local_path")
    try:
        if local_path:
            text = Path(str(local_path)).read_text(encoding="utf-8")
        else:
            url = strip_md5(str(target.get("url") or ""))
            connect_seconds = max(1.0, connect_timeout_ms / 1000)
            read_seconds = max(1.0, read_timeout_ms / 1000)
            client = HttpClient(
                {
                    "connect_timeout": connect_seconds,
                    "read_timeout": read_seconds,
                    "total_timeout": max(5.0, connect_seconds + read_seconds),
                    "max_retries": 1,
                    "user_agent": "tvbox-source-monitor/android-verify",
                }
            )
            try:
                result = client.get_json(
                    url,
                    timeout=(connect_seconds, read_seconds),
                )
            finally:
                client.close()
            if not result.ok:
                return None, f"config fetch failed: {result.error_summary()}"
            text = result.text
        document = json.loads(text)
    except Exception as error:  # noqa: BLE001 - return the reason to the report
        return None, f"config load failed: {error}"
    if not isinstance(document, dict):
        return None, "config root is not an object"
    return document, ""


def merge_isolated_reports(
    config_url: str,
    identity: str,
    site_keys: list[str],
    outcomes: list[dict | BaseException],
) -> dict:
    """Merge per-site instrumentation verdicts without inventing playback."""
    sites: list[dict] = []
    for key, outcome in zip(site_keys, outcomes):
        if isinstance(outcome, BaseException):
            sites.append(
                {
                    "key": key,
                    "ok": False,
                    "loadOk": False,
                    "playOk": False,
                    "stage": "instrumentation_crash",
                    "error": str(outcome),
                }
            )
            continue
        found = list(outcome.get("sites") or [])
        if not found:
            sites.append(
                {
                    "key": key,
                    "ok": False,
                    "loadOk": False,
                    "playOk": False,
                    "stage": str(outcome.get("stage") or "no_site_result"),
                    "error": str(outcome.get("error") or "NO_SITE_RESULT"),
                }
            )
        else:
            sites.extend(found)
    playable = any(bool(site.get("playOk")) for site in sites)
    return {
        "configUrl": config_url,
        "identity": identity,
        "ok": playable,
        "siteCount": len(site_keys),
        "sites": sites,
        "stage": "done" if playable else "NO_PLAYABLE_SITE",
        "error": "" if playable else "NO_PLAYABLE_SITE",
    }


def verify(
    serial: str,
    config_url: str,
    keyword: str,
    site_key: str,
    *,
    max_sites: int,
    connect_timeout_ms: int,
    read_timeout_ms: int,
    instrument_timeout: float,
) -> dict:
    args = [
        "shell",
        "am",
        "instrument",
        "-w",
        "-e",
        "configUrl",
        config_url,
        "-e",
        "keyword",
        keyword,
        "-e",
        "maxSites",
        str(max_sites),
        "-e",
        "connectTimeoutMs",
        str(connect_timeout_ms),
        "-e",
        "readTimeoutMs",
        str(read_timeout_ms),
    ]
    if site_key:
        args += ["-e", "siteKey", site_key]
    args.append(f"{TEST_PACKAGE}/{RUNNER}")
    # A previous run may have left a result behind.  Delete it before the
    # instrumentation starts so a timeout can never be mistaken for a verdict.
    adb(serial, "shell", "run-as", "com.tvbox.verifier", "rm", "-f", DEVICE_RESULT, check=False)
    completed = adb(serial, *args, check=False, timeout=instrument_timeout)
    cat = adb(
        serial,
        "shell",
        "run-as",
        "com.tvbox.verifier",
        "cat",
        DEVICE_RESULT,
        check=False,
    )
    raw = cat.stdout or ""
    if cat.returncode != 0 or not raw.strip():
        detail = f"{completed.stdout or ''}\n{completed.stderr or ''}".strip()
        detail = " ".join(detail.split())[-600:]
        raise RuntimeError(
            "instrumentation produced no verdict "
            f"(rc={completed.returncode}, cat_rc={cat.returncode}): {detail}"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"instrumentation returned invalid JSON: {raw[:300]!r}") from error


def summarize(verdict: dict) -> dict:
    sites = verdict.get("sites") or []
    playable = [s for s in sites if s.get("playOk")]
    loadable = [s for s in sites if s.get("loadOk")]
    return {
        "configUrl": verdict.get("configUrl", ""),
        "siteCount": verdict.get("siteCount", len(sites)),
        "loadableCount": len(loadable),
        "playableCount": len(playable),
        "playableKeys": [s.get("key") for s in playable],
    }


def verify_isolated_target(options: argparse.Namespace, target: dict) -> dict:
    """Run one instrumentation per site so one native crash stays contained."""
    document, error = load_config_document(
        target,
        connect_timeout_ms=max(1000, options.connect_timeout_ms),
        read_timeout_ms=max(1000, options.read_timeout_ms),
    )
    if document is None:
        return {
            "configUrl": target["url"],
            "ok": False,
            "stage": "config_load",
            "error": error,
            "siteCount": 0,
            "sites": [],
        }
    keys = spider_site_keys(document)
    if not keys:
        # No spider sites to isolate (for example a direct jar/class config):
        # keep the original whole-config behavior.
        return verify(
            options.serial,
            target["url"],
            options.keyword,
            options.site_key,
            max_sites=max(1, options.max_sites),
            connect_timeout_ms=max(1000, options.connect_timeout_ms),
            read_timeout_ms=max(1000, options.read_timeout_ms),
            instrument_timeout=max(10, options.instrument_timeout),
        )

    outcomes: list[dict | BaseException] = []
    for key in keys:
        if not key:
            outcomes.append(RuntimeError("site has no key; cannot isolate"))
            continue
        try:
            outcome = verify(
                options.serial,
                target["url"],
                options.keyword,
                key,
                max_sites=1,
                connect_timeout_ms=max(1000, options.connect_timeout_ms),
                read_timeout_ms=max(1000, options.read_timeout_ms),
                instrument_timeout=max(10, options.instrument_timeout),
            )
            outcomes.append(outcome)
            summary = summarize(outcome)
            print(
                json.dumps(
                    {
                        "siteKey": key,
                        "loadableCount": summary["loadableCount"],
                        "playableCount": summary["playableCount"],
                        "playableKeys": summary["playableKeys"],
                        "stage": outcome.get("stage", ""),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as crash:  # noqa: BLE001 - one bad site must not stop the rest
            outcomes.append(crash)
            print(
                json.dumps({"siteKey": key, "ok": False, "error": str(crash)}, ensure_ascii=False),
                flush=True,
            )
        finally:
            # A native crash can leave the instrumentation process wedged;
            # start every site from a clean app process.
            adb(
                options.serial,
                "shell",
                "am",
                "force-stop",
                "com.tvbox.verifier",
                check=False,
            )
    return merge_isolated_reports(target["url"], target["identity"], keys, outcomes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--config-url", action="append", default=[])
    parser.add_argument("--config-file", action="append", default=[])
    parser.add_argument(
        "--source-url",
        action="append",
        default=[],
        help="real source URL used for the recorded identity, paired by position",
    )
    parser.add_argument("--keyword", default="爱情")
    parser.add_argument("--site-key", default="")
    parser.add_argument(
        "--isolate-sites",
        action="store_true",
        help="run each spider site in its own instrumentation so one crash cannot hide the rest",
    )
    parser.add_argument("--max-sites", type=int, default=20, help="max jar/CMS sites per config")
    parser.add_argument("--connect-timeout-ms", type=int, default=10000)
    parser.add_argument("--read-timeout-ms", type=int, default=20000)
    parser.add_argument(
        "--instrument-timeout",
        type=float,
        default=180,
        help="hard wall-clock limit for one Android instrumentation run",
    )
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--record",
        nargs="?",
        const=str(DEFAULT_RECORD),
        default="",
        help="merge verdicts into data/android_verified.json (or the given path)",
    )
    options = parser.parse_args()

    if not options.config_url and not options.config_file:
        parser.error("pass at least one --config-url or --config-file")

    local_files = [Path(item) for item in options.config_file]
    root = local_files[0].resolve().parent if local_files else None
    all_passed = True
    reports = []

    origin: LocalOrigin | None = None
    try:
        if root is not None:
            origin = LocalOrigin(root)
            origin.__enter__()
            adb(options.serial, "reverse", f"tcp:{origin.port}", f"tcp:{origin.port}", check=False)
        targets = [{"url": url, "identity": url} for url in options.config_url]
        if origin is not None:
            targets += [
                {"url": origin.url_for(path), "identity": path.name, "local_path": path}
                for path in local_files
            ]
        for index, identity in enumerate(options.source_url):
            if index < len(targets):
                targets[index]["identity"] = identity
        for target in targets:
            try:
                if options.isolate_sites:
                    verdict = verify_isolated_target(options, target)
                else:
                    verdict = verify(
                        options.serial,
                        target["url"],
                        options.keyword,
                        options.site_key,
                        max_sites=max(1, options.max_sites),
                        connect_timeout_ms=max(1000, options.connect_timeout_ms),
                        read_timeout_ms=max(1000, options.read_timeout_ms),
                        instrument_timeout=max(10, options.instrument_timeout),
                    )
            except Exception as error:  # noqa: BLE001 - report, do not crash the run
                verdict = {"configUrl": target["url"], "ok": False, "error": str(error), "sites": []}
            verdict["identity"] = target["identity"]
            reports.append(verdict)
            if not verdict.get("ok"):
                all_passed = False
            print(json.dumps(summarize(verdict), ensure_ascii=False))
            if options.out:
                _write_reports(Path(options.out), reports)
            if options.record:
                _record_reports(Path(options.record), [verdict], verbose=False)
    finally:
        adb(options.serial, "shell", "am", "force-stop", "com.tvbox.verifier", check=False)
        if origin is not None:
            adb(options.serial, "reverse", "--remove", f"tcp:{origin.port}", check=False)
            origin.__exit__(None, None, None)

    if options.out:
        _write_reports(Path(options.out), reports)
    if options.record:
        recorded = _record_reports(Path(options.record), reports, verbose=False)
        if recorded:
            print(f"recorded {recorded} verdicts -> {options.record}")
    print(f"configs={len(reports)} all_passed={all_passed}")
    return 0 if all_passed else 1


def _write_reports(path: Path, reports: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_reports(path: Path, reports: list[dict], *, verbose: bool = True) -> int:
    records = []
    for report in reports:
        if not _is_verifiable_identity(report.get("identity")):
            continue
        record = _to_record(report)
        # A config that never downloaded/parsed proves nothing about its jar
        # sites; keep the file as evidence, not as a graveyard of timeouts.
        if record.site_count <= 0:
            continue
        records.append(record)
    if not records:
        return 0
    write_evidence(path, records)
    if verbose:
        print(f"recorded {len(records)} verdicts -> {path}")
    return len(records)


def _is_verifiable_identity(value: object) -> bool:
    text = str(value or "")
    return text.startswith("http://") or text.startswith("https://")


def _to_record(report: dict) -> AndroidRecord:
    summary = summarize(report)
    return AndroidRecord(
        source_id=source_id_for(str(report.get("identity"))),
        config_url=str(report.get("identity") or report.get("configUrl") or ""),
        verified_at=to_iso(),
        site_count=int(summary["siteCount"]),
        loadable_count=int(summary["loadableCount"]),
        playable_count=int(summary["playableCount"]),
        playable_keys=[str(key) for key in summary["playableKeys"]],
    )


if __name__ == "__main__":
    sys.exit(main())
