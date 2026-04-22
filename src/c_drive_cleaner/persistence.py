from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import json

from .catalog import ResolvedTarget
from .config import AppConfig, ensure_parent_dir
from .core import CleanResult, ScanStats


HISTORY_READ_LIMIT = 200


class HistoryRecord(dict):
    pass


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_scan_report(
    targets: Iterable[ResolvedTarget],
    stats: dict[str, ScanStats],
    destination: Path,
    *,
    generated_at: str | None = None,
) -> Path:
    payload_targets: list[dict[str, object]] = []
    for target in targets:
        stat = stats.get(target.key)
        payload_targets.append(
            {
                "target": _json_safe(asdict(target)),
                "stats": _json_safe(asdict(stat)) if stat is not None else None,
            }
        )

    payload = {
        "generated_at": generated_at or utc_timestamp(),
        "target_count": len(payload_targets),
        "targets": payload_targets,
    }

    ensure_parent_dir(destination)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


def append_scan_history(config: AppConfig, targets: Iterable[ResolvedTarget], stats: dict[str, ScanStats]) -> None:
    ensure_parent_dir(config.history_path)
    target_list = list(targets)
    total_bytes = sum(item.size_bytes for item in stats.values())
    total_files = sum(item.file_count for item in stats.values())
    record = {
        "type": "scan",
        "timestamp": utc_timestamp(),
        "target_count": len(target_list),
        "total_bytes": total_bytes,
        "total_files": total_files,
        "risk_counts": _risk_counts(target_list),
    }
    with config.history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history_records(path: Path, *, limit: int = HISTORY_READ_LIMIT) -> list[HistoryRecord]:
    history_path = path.expanduser()
    if not history_path.exists():
        return []

    records: list[HistoryRecord] = []
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(HistoryRecord(payload))
        if len(records) >= limit:
            break

    return records


def append_clean_history(config: AppConfig, targets: Iterable[ResolvedTarget], results: Iterable[CleanResult]) -> None:
    ensure_parent_dir(config.history_path)
    target_list = list(targets)
    result_list = list(results)
    record = {
        "type": "clean",
        "timestamp": utc_timestamp(),
        "target_count": len(target_list),
        "freed_bytes_estimate": sum(result.freed_bytes_estimate for result in result_list),
        "failed_items": sum(result.failed_items for result in result_list),
        "preserved_items": sum(result.preserved_items for result in result_list),
        "failure_reasons": _merge_failure_reasons(result_list),
        "targets": [str(target.path) for target in target_list],
        "result_details": [
            {
                "target_key": result.target_key,
                "preserved_items": result.preserved_items,
                "failure_reasons": result.failure_reasons,
                "failure_details": _json_safe([asdict(detail) for detail in result.failure_details]),
            }
            for result in result_list
            if result.failed_items or result.preserved_items
        ],
        "risk_counts": _risk_counts(target_list),
    }
    with config.history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _risk_counts(targets: Iterable[ResolvedTarget]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for target in targets:
        counts[target.risk] = counts.get(target.risk, 0) + 1
    return counts


def _merge_failure_reasons(results: Iterable[CleanResult]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for result in results:
        for reason, count in result.failure_reasons.items():
            merged[reason] = merged.get(reason, 0) + count
    return merged


def _json_safe(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value
