from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heappush, heappushpop
from pathlib import Path
from typing import Callable, Iterable
import errno
import os
import shutil

from .catalog import ResolvedTarget
from .config import AppConfig, name_matches_any, path_matches_any


LARGE_FILE_BYTES = 100 * 1024 * 1024
TOP_FILE_LIMIT = 12
FAILURE_DETAIL_LIMIT = 8


@dataclass
class FileEntry:
    path: str
    size_bytes: int


@dataclass
class FailureDetail:
    path: str
    reason: str
    message: str
    operation: str


@dataclass
class ScanStats:
    target_key: str
    exists: bool
    size_bytes: int = 0
    file_count: int = 0
    large_file_count: int = 0
    inaccessible_count: int = 0
    top_files: list[FileEntry] = field(default_factory=list)


@dataclass
class CleanResult:
    target_key: str
    deleted_items: int
    failed_items: int
    freed_bytes_estimate: int
    preserved_items: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    failure_details: list[FailureDetail] = field(default_factory=list)


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def _iter_files(root: Path) -> Iterable[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def scan_target(target: ResolvedTarget) -> ScanStats:
    path = target.path
    if not path.exists():
        return ScanStats(target_key=target.key, exists=False)

    if target.mode == "file":
        try:
            size = path.stat().st_size
        except OSError:
            return ScanStats(target_key=target.key, exists=True, inaccessible_count=1)
        return ScanStats(
            target_key=target.key,
            exists=True,
            size_bytes=size,
            file_count=1,
            large_file_count=1 if size >= LARGE_FILE_BYTES else 0,
            top_files=[FileEntry(path=str(path), size_bytes=size)],
        )

    root = path
    stats = ScanStats(target_key=target.key, exists=True)
    top_heap: list[tuple[int, str]] = []

    for file_path in _iter_files(root):
        try:
            size = file_path.stat().st_size
        except OSError:
            stats.inaccessible_count += 1
            continue

        stats.size_bytes += size
        stats.file_count += 1
        if size >= LARGE_FILE_BYTES:
            stats.large_file_count += 1

        heap_item = (size, str(file_path))
        if len(top_heap) < TOP_FILE_LIMIT:
            heappush(top_heap, heap_item)
        else:
            heappushpop(top_heap, heap_item)

    top_heap.sort(reverse=True)
    stats.top_files = [FileEntry(path=path_str, size_bytes=size) for size, path_str in top_heap]
    return stats


def scan_targets(targets: Iterable[ResolvedTarget]) -> dict[str, ScanStats]:
    return {target.key: scan_target(target) for target in targets}


def classify_failure(error: BaseException) -> str:
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, IsADirectoryError):
        return "directory_mismatch"
    if isinstance(error, NotADirectoryError):
        return "file_mismatch"

    if isinstance(error, OSError):
        if error.errno == errno.ENOTEMPTY:
            return "not_empty"
        if error.errno in {errno.EBUSY, errno.EPERM}:
            return "in_use"
        if error.errno == errno.ENAMETOOLONG:
            return "path_too_long"
        if error.errno == errno.EROFS:
            return "read_only"
        if error.errno == errno.EACCES:
            return "permission_denied"
        if getattr(error, "winerror", None) in {32, 33}:
            return "in_use"
        if getattr(error, "winerror", None) == 5:
            return "permission_denied"

    return "unknown"


def format_failure_reasons(reasons: dict[str, int]) -> str:
    if not reasons:
        return "none"
    parts = [f"{reason}={count}" for reason, count in sorted(reasons.items())]
    return ", ".join(parts)


def _record_failure(
    reasons: dict[str, int],
    details: list[FailureDetail],
    path: Path,
    operation: str,
    error: BaseException,
) -> None:
    reason = classify_failure(error)
    reasons[reason] = reasons.get(reason, 0) + 1
    if len(details) < FAILURE_DETAIL_LIMIT:
        details.append(
            FailureDetail(
                path=str(path),
                reason=reason,
                message=str(error) or error.__class__.__name__,
                operation=operation,
            )
        )


def _remove_path(
    path: Path,
    reasons: dict[str, int],
    details: list[FailureDetail],
) -> bool:
    try:
        if path.is_dir():
            errors: list[tuple[Path, BaseException]] = []

            def handle_error(function: Callable[..., object], failed_path: str, exc_info: tuple[type[BaseException], BaseException, object]) -> None:
                del function
                errors.append((Path(failed_path), exc_info[1]))

            shutil.rmtree(path, onerror=handle_error)
            if errors or path.exists():
                if not errors and path.exists():
                    errors.append((path, OSError("Directory still exists after cleanup attempt.")))
                for failed_path, error in errors:
                    _record_failure(reasons, details, failed_path, "remove_dir", error)
                return False
        else:
            path.unlink()
        return True
    except OSError as error:
        _record_failure(reasons, details, path, "remove_path", error)
        return False


def _is_preserved(path: Path, config: AppConfig) -> bool:
    return path_matches_any(path, config.preserve_path_patterns) or name_matches_any(path, config.preserve_name_patterns)


def _target_step_count(target: ResolvedTarget, config: AppConfig) -> int:
    if not target.path.exists():
        return 1
    if target.mode in {"directory", "file"}:
        return 1
    try:
        children = list(target.path.iterdir())
    except OSError:
        return 1
    if not children:
        return 1
    return sum(1 for child in children if not _is_preserved(child, config)) + sum(1 for child in children if _is_preserved(child, config))


def clean_target(
    target: ResolvedTarget,
    config: AppConfig | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
    progress_state: dict[str, int] | None = None,
) -> CleanResult:
    active_config = config or AppConfig(config_path=Path("."), export_path=Path("."), history_path=Path("."))
    state = progress_state if progress_state is not None else {"current": 0, "total": 1}

    def advance(label: str) -> None:
        state["current"] = state.get("current", 0) + 1
        if progress_callback is not None:
            progress_callback(state["current"], state.get("total", 1), label)

    if not target.path.exists():
        advance(f"跳过不存在目标: {target.path}")
        return CleanResult(target_key=target.key, deleted_items=0, failed_items=0, freed_bytes_estimate=0)

    freed_estimate = scan_target(target).size_bytes
    deleted_items = 0
    failed_items = 0
    preserved_items = 0
    failure_reasons: dict[str, int] = {}
    failure_details: list[FailureDetail] = []

    if _is_preserved(target.path, active_config):
        advance(f"保留目标: {target.path}")
        return CleanResult(
            target_key=target.key,
            deleted_items=0,
            failed_items=0,
            freed_bytes_estimate=0,
            preserved_items=1,
        )

    if target.mode == "file":
        if _remove_path(target.path, failure_reasons, failure_details):
            deleted_items = 1
        else:
            failed_items = 1
        advance(f"清理文件: {target.path.name}")
    elif target.mode == "directory":
        if _remove_path(target.path, failure_reasons, failure_details):
            deleted_items = 1
        else:
            failed_items = 1
        advance(f"清理目录: {target.path.name}")
    else:
        try:
            children = list(target.path.iterdir())
        except OSError as error:
            children = []
            failed_items += 1
            _record_failure(failure_reasons, failure_details, target.path, "list_dir", error)
            advance(f"读取目录失败: {target.path.name}")

        for child in children:
            if _is_preserved(child, active_config):
                preserved_items += 1
                advance(f"保留: {child.name}")
                continue
            if _remove_path(child, failure_reasons, failure_details):
                deleted_items += 1
            else:
                failed_items += 1
            advance(f"清理: {child.name}")

    return CleanResult(
        target_key=target.key,
        deleted_items=deleted_items,
        failed_items=failed_items,
        freed_bytes_estimate=freed_estimate,
        preserved_items=preserved_items,
        failure_reasons=failure_reasons,
        failure_details=failure_details,
    )


def clean_targets(
    targets: Iterable[ResolvedTarget],
    config: AppConfig | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[CleanResult]:
    active_config = config or AppConfig(config_path=Path("."), export_path=Path("."), history_path=Path("."))
    target_list = list(targets)
    total_steps = sum(_target_step_count(target, active_config) for target in target_list) or 1
    state = {"current": 0, "total": total_steps}
    if progress_callback is not None:
        progress_callback(0, total_steps, "准备清理...")
    return [clean_target(target, active_config, progress_callback, state) for target in target_list]
