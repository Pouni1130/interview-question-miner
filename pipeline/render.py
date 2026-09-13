"""渲染输出层:SQLite 结构化数据 → Markdown 专题文件(Jinja2 模板)。"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .config import AppConfig
from .store import Store

logger = logging.getLogger(__name__)

CATEGORY_DIRS = {
    "java_backend": "Java后端",
    "agent_ai": "Agent开发",
    "other": "其他",
}

DISCLAIMER = "内容来源于网络公开面经,版权归原作者所有,仅供个人学习。"


def _env(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render_all(cfg: AppConfig, store: Store) -> list[Path]:
    """生成/更新全部输出文件,返回写入路径列表。"""
    out_root = cfg.resolve(cfg.output.output_dir)
    env = _env(cfg.resolve(cfg.output.template_dir))
    today = datetime.now()
    month_start = today.strftime("%Y-%m-01")
    written: list[Path] = []

    # 月度面经汇总:按岗位分目录
    for category, dirname in CATEGORY_DIRS.items():
        rows = store.questions_for_render(category=category, since=month_start)
        if not rows:
            continue
        groups = _group_company_round(rows)
        companies = sorted({r["company"] or "未知公司" for r in rows})
        ctx = {
            "title": f"{dirname} 面经汇总({today.strftime('%Y-%m')})",
            "generated_at": today.isoformat(timespec="seconds"),
            "count": len(rows),
            "companies": companies,
            "groups": groups,
            "disclaimer": DISCLAIMER,
        }
        out_dir = out_root / dirname
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{today.strftime('%Y-%m')}.md"
        out.write_text(env.get_template("monthly.md.j2").render(**ctx), encoding="utf-8")
        written.append(out)

    # 近 30 天高频题 Top50
    since_30d = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = store.questions_for_render(since=since_30d)
    if rows:
        ranked = sorted(
            rows,
            key=lambda r: (r["times_seen"], r["last_seen"]),
            reverse=True,
        )[:50]
        ctx = {
            "title": f"近30天高频题 Top{len(ranked)}",
            "generated_at": today.isoformat(timespec="seconds"),
            "items": [
                {
                    "question": r["question"],
                    "q_type": r["q_type"],
                    "times_seen": r["times_seen"],
                    "company": r["company"] or "未知公司",
                    "position_category": CATEGORY_DIRS.get(
                        r["position_category"], "其他"
                    ),
                    "last_seen": (r["last_seen"] or "")[:10],
                    "source_url": r["source_url"],
                }
                for r in ranked
            ],
            "disclaimer": DISCLAIMER,
        }
        out_dir = out_root / "高频题"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "近30天高频题Top50.md"
        out.write_text(env.get_template("top.md.j2").render(**ctx), encoding="utf-8")
        written.append(out)

    logger.info("渲染完成:%d 个文件", len(written))
    return written


def _group_company_round(rows) -> list[dict]:
    """按 公司 → 轮次 两级分组,并预构建每轮次的 Markdown 文本块。"""
    by_company: dict[str, dict] = defaultdict(
        lambda: {"company": "", "rounds": defaultdict(list)}
    )
    for r in rows:
        company = r["company"] or "未知公司"
        by_company[company]["company"] = company
        round_name = r["round_name"] or "未标注轮次"
        follow_ups = _load_follow_ups(r["follow_ups"])
        by_company[company]["rounds"][round_name].append(
            {
                "question": r["question"],
                "q_type": r["q_type"],
                "follow_ups": follow_ups,
                "date": (r["round_date"] or (r["last_seen"] or ""))[:10],
                "source_url": r["source_url"],
                "post_title": r["post_title"],
            }
        )
    result = []
    for company in sorted(by_company):
        rounds = by_company[company]["rounds"]
        result.append(
            {
                "company": company,
                "rounds": [
                    {
                        "round_name": name,
                        "questions": rounds[name],
                        "block": _render_round_block(rounds[name]),
                    }
                    for name in sorted(rounds)
                ],
            }
        )
    return result


def _render_round_block(questions: list[dict]) -> str:
    """把一轮的题目渲染成嵌套列表文本(追问链缩进一层)。"""
    lines: list[str] = []
    for q in questions:
        head = f"- **{q['question']}**"
        if q["q_type"]:
            head += f" `{q['q_type']}`"
        lines.append(head)
        if q["date"]:
            lines.append(f"  - 日期:{q['date']}")
        for fu in q["follow_ups"]:
            lines.append(f"  - 追问:{fu}")
        lines.append(f"  - 来源:[{q['post_title']}]({q['source_url']})")
    return "\n".join(lines) if lines else "- (无题目记录)"


def _load_follow_ups(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def main(config_path: str | None = None) -> int:
    """独立入口:python -m pipeline.render"""
    import argparse

    from .config import load_config, setup_logging

    parser = argparse.ArgumentParser(description="渲染 Markdown 输出")
    parser.add_argument("--config", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config(config_path or args.config)
    setup_logging(cfg.resolve(cfg.output.log_dir), args.verbose)
    store = Store(cfg.resolve(cfg.output.db_path))
    store.bind_raw_root(cfg.resolve(cfg.output.raw_dir))
    for path in render_all(cfg, store):
        logger.info("已生成: %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
