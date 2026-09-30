"""Configuration loading.

Every tunable lives in ``config/*.yaml`` (spec §35-14): nothing downstream may
hard-code a threshold.  Strings may reference environment variables using
``${VAR}`` or ``${VAR:-default}``;  expansion happens at load time so the same
YAML works locally, in ``.env``, and inside GitHub Actions secrets.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILES = ("app", "regions", "scoring", "discovery", "notifications")
OPTIONAL_CONFIG_FILES = ("notifications",)

_REQUIRED_KEYS = (
    "app",
    "http",
    "output",
    "regions",
    "weights",
    "stability",
    "lifecycle",
    "candidate",
)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_MISSING = object()


class ConfigError(RuntimeError):
    """Raised when configuration is missing or internally inconsistent."""


def _sub(match: re.Match) -> str:
    name, default = match.group(1), match.group(2)
    value = os.environ.get(name)
    if value:
        return value
    if default is not None:
        return default
    return ""


def _expand(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` references inside strings."""
    if isinstance(value, str):
        return _ENV_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def _deep_merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class Config:
    """Read-only view over the merged YAML configuration."""

    def __init__(self, root: Path, data: dict[str, Any]):
        self.root = Path(root).resolve()
        self.data = data

    # -- generic accessors -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        cursor: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING or value is None:
            raise ConfigError(f"missing required config key: {dotted}")
        return value

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        if not isinstance(value, dict):
            raise ConfigError(f"config section '{name}' is missing or not a mapping")
        return value

    def section_default(self, name: str) -> dict[str, Any]:
        value = self.data.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def fail_open(self, name: str) -> bool:
        """Feature toggles are written as ``x.enabled`` and must never crash a run."""
        value = self.get(f"{name}.enabled")
        return bool(value) if value is not None else False

    # -- paths -------------------------------------------------------------
    def path(self, dotted: str, default: str | None = None, ensure_parent: bool = False) -> Path:
        raw = self.get(dotted, default)
        if raw is None:
            raise ConfigError(f"missing path config: {dotted}")
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        if ensure_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    def as_dict(self) -> dict[str, Any]:
        return self.data


def find_root(start: Path | None = None, explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / "config" / "app.yaml").is_file():
            raise ConfigError(f"{root} is not a tvbox-source-monitor root (config/app.yaml not found)")
        return root
    here = Path(start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "app.yaml").is_file():
            return candidate
    raise ConfigError("cannot locate config/app.yaml - run from the repository root or pass --root")


def _validate(cfg: Config) -> None:
    for key in _REQUIRED_KEYS:
        if not isinstance(cfg.data.get(key), dict):
            raise ConfigError(f"config section '{key}' is required and must be a mapping")

    weights = cfg.section("weights")
    total = sum(float(value) for value in weights.values())
    if abs(total - 1.0) > 0.001:
        raise ConfigError(f"scoring weights must sum to 1.0, got {total:.4f}")

    if not isinstance(cfg.get("app.db_path"), str):
        raise ConfigError("app.db_path must be a string")

    output = cfg.section("output")
    fmt = output.get("format", "multi")
    if fmt not in ("multi", "single", "sites"):
        raise ConfigError(f"output.format must be multi|single|sites, got {fmt!r}")

    thresholds = cfg.section_default("status_thresholds")
    if thresholds:
        active = float(thresholds.get("active", 70))
        degraded = float(thresholds.get("degraded", 50))
        if not 0 <= degraded < active <= 100:
            raise ConfigError("status_thresholds must satisfy 0 <= degraded < active <= 100")


def load_config(
    root: Path | None = None,
    explicit_root: str | os.PathLike | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    base = find_root(root, explicit_root)
    merged: dict[str, Any] = {}
    for name in CONFIG_FILES:
        file = base / "config" / f"{name}.yaml"
        if not file.is_file():
            if name in OPTIONAL_CONFIG_FILES:
                continue
            raise ConfigError(f"missing config file: {file}")
        with file.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{file} must contain a YAML mapping at the top level")
        merged.update(_expand(loaded))
    if overrides:
        merged = _deep_merge(merged, overrides)
    cfg = Config(base, merged)
    _validate(cfg)
    _apply_dedup_policy(cfg)
    return cfg


def _apply_dedup_policy(cfg: Config) -> None:
    """Push the §7 dedup rules into the URL layer.

    Without this the ``dedup:`` block in config/discovery.yaml would be dead
    configuration and every ``?utm_source=`` style variant of a URL would get
    its own source_id.
    """
    from .utils.urls import configure_dedup

    section = cfg.section_default("dedup")
    if not section:
        return
    # both flags must agree before any parameter is dropped - they are two
    # spellings of the same switch and the conservative reading wins
    drop_tracking = bool(section.get("strip_query_params", True)) and bool(
        section.get("drop_tracking_params", True)
    )
    tracking = section.get("tracking_params") or ()
    if not isinstance(tracking, (list, tuple, set)):
        tracking = ()
    configure_dedup(
        strip_trailing_slash=bool(section.get("strip_trailing_slash", True)),
        drop_tracking=drop_tracking,
        tracking=tracking,
    )
