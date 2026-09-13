"""统一入口:python -m pipeline {run|collect|extract|render}

单次全流程:python -m pipeline run [--days 7]
  采集(增量) → 粗过滤 → LLM 抽取 → 渲染输出
"""
from __future__ import annotations

import argparse
import logging

from .config import load_config, setup_logging

logger = logging.getLogger(__name__)


def cmd_run(cfg, days: int) -> int:
    """全流程:采集 → 粗过滤 → 抽取 → 渲染。"""
    from .collect import collect_incremental
    from .extract import coarse_filter_pass, extract_pass
    from .render import render_all
    from .store import Store

    # 1) 增量采集(内部自带限频与每日上限)
    collect_incremental(cfg)

    # 2) 粗过滤(进 LLM 前省钱)
    store = Store(cfg.resolve(cfg.output.db_path))
    store.bind_raw_root(cfg.resolve(cfg.output.raw_dir))
    coarse_filter_pass(cfg, store)

    # 3) LLM 抽取
    extract_pass(cfg, store)

    # 4) 渲染
    for path in render_all(cfg, store):
        logger.info("已生成: %s", path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description="面经自动整理流水线")
    parser.add_argument(
        "command", choices=["run", "collect", "extract", "render"], help="子命令"
    )
    parser.add_argument("--days", type=int, default=7, help="增量时间窗(天)")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.resolve(cfg.output.log_dir), args.verbose)

    if args.command == "run":
        return cmd_run(cfg, args.days)
    if args.command == "collect":
        from .collect import main as collect_main

        return collect_main()
    if args.command == "extract":
        from .extract import main as extract_main

        return extract_main()
    if args.command == "render":
        from .render import main as render_main

        return render_main()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
