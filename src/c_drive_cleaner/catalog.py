from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import os

from .config import AppConfig, name_matches_any, path_matches_any


LOW_RISK_BASENAMES = {
    "cache",
    "caches",
    "cache2",
    "cache_data",
    "cachedata",
    "cachestorage",
    "code cache",
    "dawncache",
    "gpubuffercache",
    "gpucache",
    "grshadercache",
    "service worker",
    "shadercache",
    "webcache",
}

MEDIUM_RISK_BASENAMES = {
    "crashdumps",
    "temp",
    "tmp",
}

HIGH_RISK_BASENAMES = {
    "package cache",
}

LOW_RISK_SUBSTRINGS = (
    "-cache",
    "_cache",
    ".cache",
    " cache",
    "cache ",
)

ROOT_ENV_VARS = (
    "LOCALAPPDATA",
    "APPDATA",
    "TEMP",
)

STATIC_ROOTS = (
    Path(r"C:\Windows\Temp"),
)

RISK_ORDER = {
    "low": 0,
    "medium": 1,
    "high": 2,
}


@dataclass(frozen=True)
class ResolvedTarget:
    key: str
    name: str
    path: Path
    mode: str
    safe: bool
    category: str
    notes: str
    risk: str
    source: str = "auto"


@dataclass(frozen=True)
class RiskAssessment:
    risk: str
    category: str
    notes: str


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()

    for env_name in ROOT_ENV_VARS:
        raw = os.environ.get(env_name)
        if not raw:
            continue
        path = Path(raw).expanduser()
        normalized = str(path).lower()
        if normalized in seen or not path.exists():
            continue
        roots.append(path)
        seen.add(normalized)

    for path in STATIC_ROOTS:
        normalized = str(path).lower()
        if normalized in seen or not path.exists():
            continue
        roots.append(path)
        seen.add(normalized)

    return roots


def _assess_cache_dir(path: Path) -> RiskAssessment | None:
    name = path.name.strip().lower()
    if not name:
        return None

    if name in HIGH_RISK_BASENAMES:
        return RiskAssessment("high", "安装包", "看起来像安装包或软件包缓存，删除前建议先确认。")

    if name in LOW_RISK_BASENAMES:
        return RiskAssessment("low", "缓存", "典型缓存目录名称。")

    if name in MEDIUM_RISK_BASENAMES:
        return RiskAssessment("medium", "临时文件", "通常可以清理，但也可能正在被程序占用。")

    if any(token in name for token in LOW_RISK_SUBSTRINGS):
        return RiskAssessment("medium", "缓存", "根据目录名称模式识别出的缓存候选目录。")

    return None


def _walk_candidate_dirs(root: Path) -> Iterable[tuple[Path, RiskAssessment]]:
    root_assessment = _assess_cache_dir(root)
    if root_assessment is not None:
        yield root, root_assessment
        return

    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                child_dirs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child_dirs.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue

        for child_path in child_dirs:
            assessment = _assess_cache_dir(child_path)
            if assessment is not None:
                yield child_path, assessment
                continue
            stack.append(child_path)


def _dedupe_nested_paths(paths: Iterable[tuple[Path, RiskAssessment]]) -> list[tuple[Path, RiskAssessment]]:
    unique: dict[str, tuple[Path, RiskAssessment]] = {}
    for path, assessment in paths:
        resolved = path.resolve(strict=False)
        normalized = str(resolved).lower()
        current = unique.get(normalized)
        if current is None or RISK_ORDER[assessment.risk] > RISK_ORDER[current[1].risk]:
            unique[normalized] = (resolved, assessment)

    ordered = sorted(unique.values(), key=lambda item: (len(item[0].parts), str(item[0]).lower()))
    kept: list[tuple[Path, RiskAssessment]] = []
    for path, assessment in ordered:
        if any(path.is_relative_to(existing_path) for existing_path, _ in kept):
            continue
        kept.append((path, assessment))
    return kept


def _display_name(path: Path, roots: Iterable[Path]) -> str:
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            return path.name or str(path)
        if len(relative.parts) >= 2:
            return f"{relative.parts[-2]} / {relative.parts[-1]}"
        return relative.parts[-1]
    return path.name or str(path)


def _is_ignored(path: Path, assessment: RiskAssessment, config: AppConfig) -> bool:
    if config.whitelist_path_patterns and not path_matches_any(path, config.whitelist_path_patterns):
        return True
    if assessment.risk.lower() in config.ignored_risk_levels:
        return True
    if name_matches_any(path, config.ignored_name_patterns):
        return True
    return path_matches_any(path, config.ignored_path_patterns)


def resolve_default_targets(config: AppConfig) -> list[ResolvedTarget]:
    roots = _candidate_roots()
    discovered: list[tuple[Path, RiskAssessment]] = []
    for root in roots:
        discovered.extend(_walk_candidate_dirs(root))

    targets: list[ResolvedTarget] = []
    for path, assessment in _dedupe_nested_paths(discovered):
        if _is_ignored(path, assessment, config):
            continue
        normalized = str(path).lower()
        safe = assessment.risk == "low"
        targets.append(
            ResolvedTarget(
                key=f"auto:{normalized}",
                name=_display_name(path, roots),
                path=path,
                mode="contents",
                safe=safe,
                category=assessment.category,
                notes=assessment.notes,
                risk=assessment.risk,
            )
        )
    return targets


def expand_file_targets(targets: Iterable[ResolvedTarget]) -> list[ResolvedTarget]:
    file_targets: list[ResolvedTarget] = []
    seen: set[str] = set()

    for target in targets:
        stack = [target.path]
        while stack:
            current = stack.pop()
            if not current.exists():
                continue
            if current.is_file():
                normalized = str(current).lower()
                if normalized in seen:
                    continue
                seen.add(normalized)
                file_targets.append(
                    ResolvedTarget(
                        key=f"file:{normalized}",
                        name=current.name,
                        path=current,
                        mode="file",
                        safe=target.safe,
                        category=target.category,
                        notes=f"{target.notes} 来源目录: {target.path}",
                        risk=target.risk,
                        source="auto-file",
                    )
                )
                continue

            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            entry_path = Path(entry.path)
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry_path)
                            elif entry.is_file(follow_symlinks=False):
                                normalized = str(entry_path).lower()
                                if normalized in seen:
                                    continue
                                seen.add(normalized)
                                file_targets.append(
                                    ResolvedTarget(
                                        key=f"file:{normalized}",
                                        name=entry_path.name,
                                        path=entry_path,
                                        mode="file",
                                        safe=target.safe,
                                        category=target.category,
                                        notes=f"{target.notes} 来源目录: {target.path}",
                                        risk=target.risk,
                                        source="auto-file",
                                    )
                                )
                        except OSError:
                            continue
            except OSError:
                continue

    return file_targets


def safe_target_keys(targets: Iterable[ResolvedTarget]) -> set[str]:
    return {target.key for target in targets if target.safe}
