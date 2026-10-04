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

Local files are served to the device over ``adb reverse`` so candidates that
have not been published yet can be verified too.  Exit code is 0 when at least
one site in every config reached a media URL, 1 otherwise.

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checks.jar_check import AndroidRecord, write_evidence  # noqa: E402
from app.utils.timeutil import to_iso  # noqa: E402
from app.utils.urls import source_id_for  # noqa: E402

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
    adb(serial, *args, check=False, timeout=instrument_timeout)
    raw = adb(serial, "shell", "run-as", "com.tvbox.verifier", "cat", DEVICE_RESULT).stdout
    return json.loads(raw)


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
                {"url": origin.url_for(path), "identity": path.name}
                for path in local_files
            ]
        for index, identity in enumerate(options.source_url):
            if index < len(targets):
                targets[index]["identity"] = identity
        for target in targets:
            try:
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
