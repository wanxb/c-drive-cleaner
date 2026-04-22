from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Static

from .catalog import ResolvedTarget, expand_file_targets, resolve_default_targets, safe_target_keys
from .config import AppConfig, load_app_config, save_default_config
from .core import CleanResult, ScanStats, clean_targets, format_bytes, scan_targets


@dataclass
class Summary:
    target_count: int = 0
    total_bytes: int = 0
    total_files: int = 0
    large_files: int = 0
    low_risk: int = 0
    medium_risk: int = 0
    high_risk: int = 0


RISK_LABELS = {
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
}

FAILURE_REASON_LABELS = {
    "permission_denied": "权限不足",
    "not_found": "目标不存在",
    "directory_mismatch": "目录类型不匹配",
    "file_mismatch": "文件类型不匹配",
    "not_empty": "目录非空",
    "in_use": "正在使用中",
    "path_too_long": "路径过长",
    "read_only": "只读",
    "unknown": "未知错误",
    "none": "无",
}

OPERATION_LABELS = {
    "remove_dir": "删除目录",
    "remove_path": "删除路径",
    "list_dir": "读取目录",
}

SPINNER_FRAMES = ["|", "/", "-", "\\"]
BAR_WIDTH = 28


def _risk_text(value: str) -> str:
    return RISK_LABELS.get(value, value)


def _failure_reason_text(value: str) -> str:
    return FAILURE_REASON_LABELS.get(value, value)


def _failure_reasons_text(reasons: dict[str, int]) -> str:
    if not reasons:
        return "无"
    parts = [f"{_failure_reason_text(reason)}={count}" for reason, count in sorted(reasons.items())]
    return "，".join(parts)


def _operation_text(value: str) -> str:
    return OPERATION_LABELS.get(value, value)


def _render_checker_bar(current: int, total: int, *, animated_offset: int = 0) -> str:
    safe_total = max(total, 1)
    safe_current = max(0, min(current, safe_total))
    filled = int((safe_current / safe_total) * BAR_WIDTH)
    cells: list[str] = []
    for index in range(BAR_WIDTH):
        if index < filled:
            cells.append("=" if (index + animated_offset) % 2 == 0 else "#")
        else:
            cells.append("-" if (index + animated_offset) % 2 == 0 else " ")
    return "".join(cells)


def _render_marquee_bar(position: int) -> str:
    cells = [" "] * BAR_WIDTH
    block_width = 6
    start = position % (BAR_WIDTH + block_width)
    for index in range(block_width):
        cursor = start - index
        if 0 <= cursor < BAR_WIDTH:
            cells[cursor] = "=" if index % 2 == 0 else "#"
    return "".join(cells)


def _render_status_line(message: str, bar: str, suffix: str = "") -> str:
    tail = f" {suffix}" if suffix else ""
    return f"{message} [{bar}]{tail}"


class ResizableDataTable(DataTable):
    RESIZE_HITBOX = 2
    MIN_CONTENT_WIDTH = 4

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._resizing_column_index: int | None = None
        self._resize_start_x: float = 0
        self._resize_start_width: int = 0

    def _resize_column_at(self, x: float, y: float) -> int | None:
        if y < 0:
            return None
        pointer_x = x
        for column_index in range(len(self.ordered_columns)):
            region = self._get_column_region(column_index)
            right_edge = region.x + region.width - 1
            if abs(pointer_x - right_edge) <= self.RESIZE_HITBOX:
                return column_index
        return None

    def _set_column_render_width(self, column_index: int, render_width: int) -> None:
        if not self.is_valid_column_index(column_index):
            return
        column_key = self._column_locations.get_key(column_index)
        column = self.columns[column_key]
        content_width = max(self.MIN_CONTENT_WIDTH, render_width - 2 * self.cell_padding)
        column.auto_width = False
        column.width = content_width
        self._require_update_dimensions = True
        self.refresh(layout=True)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        column_index = self._resize_column_at(event.x, event.y)
        if column_index is not None:
            region = self._get_column_region(column_index)
            self._resizing_column_index = column_index
            self._resize_start_x = event.x
            self._resize_start_width = region.width
            self.capture_mouse()
            event.stop()
            event.prevent_default()
            return
        await super()._on_mouse_down(event)

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        if self._resizing_column_index is not None:
            delta = int(round(event.x - self._resize_start_x))
            self._set_column_render_width(self._resizing_column_index, self._resize_start_width + delta)
            event.stop()
            event.prevent_default()
            return
        super()._on_mouse_move(event)

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if self._resizing_column_index is not None:
            self._resizing_column_index = None
            self.release_mouse()
            event.stop()
            event.prevent_default()
            return
        await super()._on_mouse_up(event)


class ConfirmCleanScreen(ModalScreen[bool]):
    CSS = """
    ConfirmCleanScreen {
        align: center middle;
    }

    #confirm-dialog {
        width: 72;
        height: auto;
        border: solid #4a4a4a;
        background: #222222;
        padding: 1 2;
    }

    #confirm-actions {
        height: auto;
        margin-top: 1;
    }

    Button {
        background: #303030;
        color: #d8d8d8;
        border: solid #4a4a4a;
    }

    Button:hover {
        background: #3a3a3a;
    }
    """

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(self.title)
            yield Static(self.message)
            with Horizontal(id="confirm-actions"):
                yield Button("取消", id="cancel")
                yield Button("确认", id="confirm")

    @on(Button.Pressed, "#cancel")
    def handle_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def handle_confirm(self) -> None:
        self.dismiss(True)


class CleanerApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #toolbar {
        height: auto;
        padding: 1 1;
        background: $surface;
    }

    #content {
        height: 1fr;
    }

    #left {
        width: 3fr;
        min-width: 88;
        height: 1fr;
    }

    #right {
        width: 1fr;
        min-width: 42;
        height: 1fr;
        border-left: solid $primary;
        padding: 0 1;
    }

    #summary {
        height: 5;
        padding: 1 1;
        background: $boost;
        margin-bottom: 1;
    }

    #details {
        height: 11;
        padding: 1;
        background: $panel;
        margin-bottom: 1;
    }

    #status-label {
        width: 1fr;
        height: 1;
        padding: 0 1;
        margin-top: 2;
        text-style: bold;
    }



    #log {
        height: 1fr;
        padding: 1;
        background: $surface;
    }

    DataTable {
        height: 1fr;
    }

    .toolbar-row {
        height: auto;
        margin-bottom: 0;
        align: left bottom;
    }

    Button {
        background: #303030;
        color: #d8d8d8;
        border: solid #4a4a4a;
    }

    Button:hover {
        background: #3a3a3a;
    }
    """

    TITLE = "C 盘清理工具"
    SUB_TITLE = "自动发现缓存目录"

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("r", "scan", "重新扫描"),
        Binding("space", "toggle_mark", "标记"),
        Binding("c", "clean_selected", "清理当前"),
        Binding("a", "clean_safe", "清理低风险"),
        Binding("t", "toggle_view_mode", "切换视图"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: AppConfig = load_app_config()
        self.targets: dict[str, ResolvedTarget] = {}
        self.stats: dict[str, ScanStats] = {}
        self.last_clean_results: dict[str, CleanResult] = {}
        self.marked: set[str] = set()
        self.visible_keys: list[str] = []
        self.log_lines: list[str] = []
        self.view_mode: Literal["folders", "files"] = "folders"
        self.busy_mode: Literal["idle", "querying", "cleaning"] = "idle"
        self.spinner_index = 0
        self.query_progress_value = 0
        self.query_marquee_position = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="toolbar"):
            with Horizontal(classes="toolbar-row"):
                yield Button("扫描", id="scan")
                yield Button("切换到文件视图", id="toggle-view")
                yield Button("标记/取消", id="toggle")
                yield Button("清理当前", id="clean-selected")
                yield Button("清理已标记", id="clean-marked")
                yield Button("清理低风险", id="clean-safe")
                yield Static("???????", id="status-label")
        with Horizontal(id="content"):
            with Vertical(id="left"):
                yield ResizableDataTable(id="targets")
            with Vertical(id="right"):
                yield Static(id="summary")
                yield Static(id="details")
                yield ResizableDataTable(id="files")
                yield Static(id="log")
        yield Footer()

    def on_mount(self) -> None:
        self._init_tables()
        if not self.config.config_path.exists():
            save_default_config(self.config)
        self._set_idle_status("当前没有任务。")
        self.set_interval(0.12, self._tick_spinner)
        self._log(f"配置文件: {self.config.config_path}")
        self._set_querying_status("正在初始化扫描，请稍候...")
        self.run_scan()

    def _init_tables(self) -> None:
        target_table = self.query_one("#targets", DataTable)
        target_table.cursor_type = "row"
        target_table.zebra_stripes = True
        target_table.add_columns("标记", "名称", "风险", "安全", "类别", "大小", "文件数", "大文件", "状态", "路径")

        file_table = self.query_one("#files", DataTable)
        file_table.cursor_type = "row"
        file_table.zebra_stripes = True
        file_table.add_columns("大小", "路径")

    def _summary_for_visible_targets(self) -> Summary:
        summary = Summary(target_count=len(self.visible_keys))
        for key in self.visible_keys:
            target = self.targets.get(key)
            if target is None:
                continue
            if target.risk == "low":
                summary.low_risk += 1
            elif target.risk == "medium":
                summary.medium_risk += 1
            elif target.risk == "high":
                summary.high_risk += 1

            stats = self.stats.get(key)
            if stats is None:
                continue
            summary.total_bytes += stats.size_bytes
            summary.total_files += stats.file_count
            summary.large_files += stats.large_file_count
        return summary

    def _tick_spinner(self) -> None:
        if self.busy_mode != "querying":
            return
        self.spinner_index = (self.spinner_index + 1) % len(SPINNER_FRAMES)
        label = self.query_one("#status-label", Static)
        current = getattr(self, "_query_message", "正在查询缓存项目，请稍候...")
        self.query_marquee_position = (self.query_marquee_position + 1) % (BAR_WIDTH + 6)
        label.update(
            _render_status_line(
                f"{SPINNER_FRAMES[self.spinner_index]} {current}",
                _render_marquee_bar(self.query_marquee_position),
            )
        )

    def _set_idle_status(self, message: str) -> None:
        label = self.query_one("#status-label", Static)
        label.update(message)
        self.busy_mode = "idle"
        self.query_progress_value = 0
        self.query_marquee_position = 0
        self._set_buttons_disabled(False)

    def _set_querying_status(self, message: str) -> None:
        label = self.query_one("#status-label", Static)
        self._query_message = message
        self.query_marquee_position = 0
        label.update(
            _render_status_line(
                f"{SPINNER_FRAMES[self.spinner_index]} {message}",
                _render_marquee_bar(self.query_marquee_position),
            )
        )
        self.busy_mode = "querying"
        self._set_buttons_disabled(True)

    def _set_progress(self, current: int, total: int, message: str) -> None:
        label = self.query_one("#status-label", Static)
        safe_total = max(total, 1)
        safe_current = max(0, min(current, safe_total))
        percent = int((safe_current / safe_total) * 100)
        label.update(
            _render_status_line(
                f"正在清理 {safe_current}/{safe_total}",
                _render_checker_bar(safe_current, safe_total),
                f"{percent}% | {message}",
            )
        )
        self.busy_mode = "cleaning"
        self._set_buttons_disabled(True)

    def _set_buttons_disabled(self, disabled: bool) -> None:
        for button_id in ("#scan", "#toggle-view", "#toggle", "#clean-selected", "#clean-marked", "#clean-safe"):
            self.query_one(button_id, Button).disabled = disabled

    def _refresh_view_button(self) -> None:
        button = self.query_one("#toggle-view", Button)
        if self.view_mode == "folders":
            button.label = "切换到文件视图"
        else:
            button.label = "切换到文件夹视图"

    def _should_hide_target(self, target: ResolvedTarget, stats: ScanStats | None) -> bool:
        del target
        if stats is None or not stats.exists:
            return False
        return stats.size_bytes <= 0

    def _refresh_target_table(self) -> None:
        table = self.query_one("#targets", DataTable)
        table.clear()
        self.visible_keys = []

        sorted_targets = sorted(
            self.targets.values(),
            key=lambda target: self.stats.get(target.key, ScanStats(target.key, False)).size_bytes,
            reverse=True,
        )

        for target in sorted_targets:
            stats = self.stats.get(target.key)
            if self._should_hide_target(target, stats):
                continue
            self.visible_keys.append(target.key)

            status = "未扫描"
            size = "-"
            files = "-"
            large = "-"
            if stats is not None:
                if not stats.exists:
                    status = "不存在"
                    size = "0 B"
                    files = "0"
                    large = "0"
                else:
                    status = "就绪"
                    size = format_bytes(stats.size_bytes)
                    files = str(stats.file_count)
                    large = str(stats.large_file_count)

            table.add_row(
                "x" if target.key in self.marked else "",
                target.name,
                _risk_text(target.risk),
                "是" if target.safe else "需确认",
                target.category,
                size,
                files,
                large,
                status,
                str(target.path),
            )

        self._refresh_view_button()
        self._refresh_summary()
        self._refresh_details()

    def _refresh_summary(self) -> None:
        summary = self._summary_for_visible_targets()
        view_text = "缓存文件夹" if self.view_mode == "folders" else "缓存文件"
        text = (
            f"视图: {view_text} | 目标: {summary.target_count} | 低风险: {summary.low_risk} | 中风险: {summary.medium_risk} | 高风险: {summary.high_risk}\n"
            f"预计可回收: {format_bytes(summary.total_bytes)}\n"
            f"文件总数: {summary.total_files} | 大文件(>=100MB): {summary.large_files}\n"
            f"白名单: {len(self.config.whitelist_path_patterns)} | 忽略规则: {len(self.config.ignored_path_patterns) + len(self.config.ignored_name_patterns) + len(self.config.ignored_risk_levels)}"
            f" | 保留规则: {len(self.config.preserve_path_patterns) + len(self.config.preserve_name_patterns)}"
        )
        self.query_one("#summary", Static).update(text)

    def _current_target(self) -> ResolvedTarget | None:
        table = self.query_one("#targets", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self.visible_keys):
            return None
        key = self.visible_keys[table.cursor_row]
        return self.targets.get(key)

    def _refresh_details(self) -> None:
        target = self._current_target()
        files_table = self.query_one("#files", DataTable)
        files_table.clear()

        if target is None:
            self.query_one("#details", Static).update("请选择一个目标查看详情。")
            return

        stats = self.stats.get(target.key)
        if stats is None:
            detail_text = f"{target.name}\n{target.path}\n尚未扫描。"
        else:
            mode_text = "文件" if target.mode == "file" else "文件夹"
            detail_text = (
                f"{target.name}\n"
                f"{target.path}\n"
                f"类型: {mode_text} | 风险: {_risk_text(target.risk)} | 可批量清理: {'是' if target.safe else '否'} | 类别: {target.category}\n"
                f"预计大小: {format_bytes(stats.size_bytes)} | 文件数: {stats.file_count} | 大文件: {stats.large_file_count}\n"
                f"不可访问项: {stats.inaccessible_count}\n"
                f"说明: {target.notes}"
            )

            clean_result = self.last_clean_results.get(target.key)
            if clean_result is not None:
                detail_text += (
                    f"\n上次清理: 删除 {clean_result.deleted_items}，失败 {clean_result.failed_items}"
                    f"，保留 {clean_result.preserved_items}"
                    f" | 失败原因: {_failure_reasons_text(clean_result.failure_reasons)}"
                )
                for detail in clean_result.failure_details[:3]:
                    detail_text += f"\n- {_failure_reason_text(detail.reason)} / {_operation_text(detail.operation)}: {detail.path}"

            for file_entry in stats.top_files:
                if file_entry.size_bytes <= 0:
                    continue
                files_table.add_row(format_bytes(file_entry.size_bytes), file_entry.path)

        self.query_one("#details", Static).update(detail_text)

    def _log(self, message: str) -> None:
        log_widget = self.query_one("#log", Static)
        self.log_lines.insert(0, message)
        self.log_lines = self.log_lines[:100]
        log_widget.update("\n".join(self.log_lines))

    def _selected_targets(self, keys: list[str]) -> list[ResolvedTarget]:
        return [self.targets[key] for key in keys if key in self.targets]

    def _request_clean(self, keys: list[str], label: str) -> None:
        selected_targets = self._selected_targets(keys)
        if not selected_targets:
            self._log("没有选中的目标。")
            return

        selected_stats = [self.stats.get(target.key, ScanStats(target.key, False)) for target in selected_targets]
        total_bytes = sum(stat.size_bytes for stat in selected_stats)
        high_risk = sum(1 for target in selected_targets if target.risk == "high")
        medium_risk = sum(1 for target in selected_targets if target.risk == "medium")

        if not self.config.confirm_before_clean:
            self._log(f"开始{label}，共 {len(selected_targets)} 项。")
            self.run_clean(keys)
            return

        message = (
            f"{label}\n"
            f"目标数: {len(selected_targets)}\n"
            f"预计可回收: {format_bytes(total_bytes)}\n"
            f"中风险: {medium_risk} | 高风险: {high_risk}"
        )
        self.push_screen(
            ConfirmCleanScreen("确认清理", message),
            lambda confirmed: self._on_clean_confirmation(confirmed, keys, label),
        )

    def _on_clean_confirmation(self, confirmed: bool, keys: list[str], label: str) -> None:
        if not confirmed:
            self._log(f"已取消 {label}")
            return
        self._log(f"开始{label}...")
        self._set_progress(0, max(len(keys), 1), "正在准备清理...")
        self.run_clean(keys)

    def _progress_callback(self, current: int, total: int, message: str) -> None:
        self.call_from_thread(self._set_progress, current, total, message)

    @work(exclusive=True, thread=True)
    def run_scan(self) -> None:
        folder_targets = resolve_default_targets(self.config)
        snapshot = folder_targets if self.view_mode == "folders" else expand_file_targets(folder_targets)
        results = scan_targets(snapshot)
        self.call_from_thread(self._finish_scan, snapshot, results)

    def _finish_scan(self, targets: list[ResolvedTarget], results: dict[str, ScanStats]) -> None:
        self.targets = {target.key: target for target in targets}
        self.marked.intersection_update(self.targets)
        self.stats = results
        self._refresh_target_table()
        self._set_idle_status(f"扫描完成，发现 {len(self.visible_keys)} 个候选项。")
        mode_text = "文件夹" if self.view_mode == "folders" else "文件"
        self._log(f"扫描完成，发现 {len(self.visible_keys)} 个缓存{mode_text}候选项。")

    @work(exclusive=True, thread=True)
    def run_clean(self, keys: list[str]) -> None:
        selected_targets = self._selected_targets(keys)
        results = clean_targets(selected_targets, self.config, self._progress_callback)
        self.call_from_thread(self._finish_clean, results, keys)

    def _finish_clean(self, results: list[CleanResult], cleaned_keys: list[str]) -> None:
        freed = sum(result.freed_bytes_estimate for result in results)
        failures = sum(result.failed_items for result in results)
        preserved = sum(result.preserved_items for result in results)
        merged_failure_reasons: dict[str, int] = {}
        for result in results:
            self.last_clean_results[result.target_key] = result
            for reason, count in result.failure_reasons.items():
                merged_failure_reasons[reason] = merged_failure_reasons.get(reason, 0) + count
        for key in cleaned_keys:
            self.marked.discard(key)

        message = f"清理完成，共 {len(cleaned_keys)} 项。预计释放 {format_bytes(freed)}。失败: {failures}。保留: {preserved}。"
        if merged_failure_reasons:
            message += f" 原因: {_failure_reasons_text(merged_failure_reasons)}。"
        self._log(message)
        self._set_progress(1, 1, "清理完成，正在刷新结果...")
        self._set_querying_status("正在刷新扫描结果，请稍候...")
        self.run_scan()

    @on(Button.Pressed, "#scan")
    def handle_scan(self) -> None:
        self.action_scan()

    @on(Button.Pressed, "#toggle-view")
    def handle_toggle_view(self) -> None:
        self.action_toggle_view_mode()

    @on(Button.Pressed, "#toggle")
    def handle_toggle(self) -> None:
        self.action_toggle_mark()

    @on(Button.Pressed, "#clean-selected")
    def handle_clean_selected(self) -> None:
        self.action_clean_selected()

    @on(Button.Pressed, "#clean-marked")
    def handle_clean_marked(self) -> None:
        if self.marked:
            self._request_clean(sorted(self.marked), "清理已标记项")
        else:
            self._log("没有已标记的目标。")

    @on(Button.Pressed, "#clean-safe")
    def handle_clean_safe(self) -> None:
        self.action_clean_safe()

    @on(DataTable.RowSelected, "#targets")
    def handle_row_selected(self) -> None:
        self._refresh_details()

    @on(DataTable.RowHighlighted, "#targets")
    def handle_row_highlighted(self) -> None:
        self._refresh_details()

    def action_scan(self) -> None:
        if self.busy_mode != "idle":
            self._log("当前正在处理任务，请稍候。")
            return
        self._log("正在扫描...")
        self._set_querying_status("正在查询缓存项目，请稍候...")
        self.run_scan()

    def action_toggle_view_mode(self) -> None:
        if self.busy_mode != "idle":
            self._log("当前正在处理任务，请稍候。")
            return
        self.view_mode = "files" if self.view_mode == "folders" else "folders"
        self.marked.clear()
        self._log(f"已切换到{'缓存文件' if self.view_mode == 'files' else '缓存文件夹'}视图。")
        self._set_querying_status("正在查询缓存项目，请稍候...")
        self.run_scan()

    def action_toggle_mark(self) -> None:
        if self.busy_mode != "idle":
            self._log("当前正在处理任务，请稍候。")
            return
        target = self._current_target()
        if target is None:
            self._log("当前没有选中目标。")
            return
        if target.key in self.marked:
            self.marked.remove(target.key)
        else:
            self.marked.add(target.key)
        self._refresh_target_table()

    def action_clean_selected(self) -> None:
        if self.busy_mode != "idle":
            self._log("当前正在处理任务，请稍候。")
            return
        target = self._current_target()
        if target is None:
            self._log("当前没有选中目标。")
            return
        self._request_clean([target.key], f"清理当前目标: {target.path}")

    def action_clean_safe(self) -> None:
        if self.busy_mode != "idle":
            self._log("当前正在处理任务，请稍候。")
            return
        keys = sorted(safe_target_keys(self.targets.values()))
        if not keys:
            self._log("没有可批量清理的低风险目标。")
            return
        self._request_clean(keys, f"清理全部低风险目标 ({len(keys)})")


def run() -> None:
    CleanerApp().run()
