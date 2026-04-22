from __future__ import annotations

import argparse

from .app import run
from .catalog import resolve_default_targets
from .config import load_app_config, save_default_config
from .core import format_bytes, scan_targets


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows 缓存清理工具。")
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="执行一次扫描，并按体积输出自动发现的缓存目录。",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="如果配置文件不存在，则生成默认配置文件。",
    )
    args = parser.parse_args()

    config = load_app_config()

    if args.init_config:
        if config.config_path.exists():
            print(f"Config already exists: {config.config_path}")
        else:
            save_default_config(config)
            print(f"Created default config: {config.config_path}")
        if not args.scan_once:
            return

    if args.scan_once:
        targets = resolve_default_targets(config)
        stats = scan_targets(targets)

        rows = sorted(
            ((target, stats[target.key]) for target in targets),
            key=lambda item: item[1].size_bytes,
            reverse=True,
        )
        for target, stat in rows:
            print(
                f"{format_bytes(stat.size_bytes):>10} | {stat.file_count:>8} files | "
                f"{stat.large_file_count:>5} large | {target.risk:>6} | {'safe' if target.safe else 'review':>6} | {target.path}"
            )
        return

    if not config.config_path.exists():
        save_default_config(config)
        print(f"Created default config: {config.config_path}")

    run()


if __name__ == "__main__":
    main()
