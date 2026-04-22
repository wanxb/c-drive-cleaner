from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
import json
import os


APP_DIR_NAME = "c-drive-cleaner"
CONFIG_FILE_NAME = "config.json"
DEFAULT_EXPORT_FILE_NAME = "scan-latest.json"
DEFAULT_HISTORY_FILE_NAME = "history.jsonl"


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    export_path: Path
    history_path: Path
    confirm_before_clean: bool = True
    whitelist_path_patterns: tuple[str, ...] = field(default_factory=tuple)
    ignored_path_patterns: tuple[str, ...] = field(default_factory=tuple)
    ignored_name_patterns: tuple[str, ...] = field(default_factory=tuple)
    ignored_risk_levels: tuple[str, ...] = field(default_factory=tuple)
    preserve_path_patterns: tuple[str, ...] = field(default_factory=tuple)
    preserve_name_patterns: tuple[str, ...] = field(default_factory=tuple)


def _app_home() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.cwd() / f".{APP_DIR_NAME}"


def default_config_path() -> Path:
    return _app_home() / CONFIG_FILE_NAME


def _normalize_patterns(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    normalized: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            normalized.append(value.strip())
    return tuple(normalized)


def load_app_config(path: Path | None = None) -> AppConfig:
    config_path = (path or default_config_path()).expanduser()
    app_home = config_path.parent
    raw: dict[str, object] = {}

    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}

    export_path = app_home / DEFAULT_EXPORT_FILE_NAME
    history_path = app_home / DEFAULT_HISTORY_FILE_NAME

    if isinstance(raw.get("export_path"), str) and raw["export_path"]:
        export_path = Path(str(raw["export_path"])).expanduser()
    if isinstance(raw.get("history_path"), str) and raw["history_path"]:
        history_path = Path(str(raw["history_path"])).expanduser()

    confirm_before_clean = raw.get("confirm_before_clean", True)
    if not isinstance(confirm_before_clean, bool):
        confirm_before_clean = True

    return AppConfig(
        config_path=config_path,
        export_path=export_path,
        history_path=history_path,
        confirm_before_clean=confirm_before_clean,
        whitelist_path_patterns=_normalize_patterns(raw.get("whitelist_path_patterns")),
        ignored_path_patterns=_normalize_patterns(raw.get("ignored_path_patterns")),
        ignored_name_patterns=_normalize_patterns(raw.get("ignored_name_patterns")),
        ignored_risk_levels=tuple(value.lower() for value in _normalize_patterns(raw.get("ignored_risk_levels"))),
        preserve_path_patterns=_normalize_patterns(raw.get("preserve_path_patterns")),
        preserve_name_patterns=_normalize_patterns(raw.get("preserve_name_patterns")),
    )


def ensure_parent_dir(path: Path) -> None:
    path.expanduser().parent.mkdir(parents=True, exist_ok=True)


def save_default_config(config: AppConfig) -> None:
    ensure_parent_dir(config.config_path)
    payload = {
        "confirm_before_clean": config.confirm_before_clean,
        "whitelist_path_patterns": list(config.whitelist_path_patterns),
        "ignored_path_patterns": list(config.ignored_path_patterns),
        "ignored_name_patterns": list(config.ignored_name_patterns),
        "ignored_risk_levels": list(config.ignored_risk_levels),
        "preserve_path_patterns": list(config.preserve_path_patterns),
        "preserve_name_patterns": list(config.preserve_name_patterns),
        "export_path": str(config.export_path),
        "history_path": str(config.history_path),
    }
    config.config_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def path_matches_any(path: Path, patterns: tuple[str, ...]) -> bool:
    full_path = str(path).lower()
    return any(fnmatch(full_path, pattern.lower()) for pattern in patterns)


def name_matches_any(path: Path, patterns: tuple[str, ...]) -> bool:
    name = path.name.lower()
    return any(fnmatch(name, pattern.lower()) for pattern in patterns)
