"""Evidence-preserving monthly Markdown and distinct-post recency ranking."""
from __future__ import annotations
import json
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from .config import AppConfig
from .runtime import now

CATEGORY_DIRS = {"java_backend":"Java后端","agent_ai":"Agent开发","other":"其他"}
DISCLAIMER = "内容来源于网络公开面经，版权归原作者所有，仅供个人学习。"

def md(value) -> str:
    return re.sub(r"([\\`*_{}\[\]<>|])",r"\\\1",str(value or "").replace("\n"," ").replace("\r"," "))

def _group_company_round(rows) -> list[dict]:
    groups = defaultdict(lambda:defaultdict(list))
    for row in rows:
        date_label = "面试日期" if row["round_date"] else "发布日期（面试日期未知）"
        lines = [f"- **{md(row['question'])}** `{md(row['q_type'])}`",
                 f"  - {date_label}：{row['event_date']}"]
        for follow in json.loads(row["follow_ups"]):
            lines.append(f"  - 追问：{md(follow)}")
        lines.append(f"  - 来源：[{md(row['post_title'])}]({row['source_url']})")
        groups[row["company"] or "未知公司"][row["round_name"] or "未标注轮次"].append("\n".join(lines))
    return [{"company":company,"rounds":[{"round_name":name,"block":"\n".join(items)} for name,items in sorted(rounds.items())]} for company,rounds in sorted(groups.items())]

def rank_questions(rows, cfg: AppConfig, today: date | None = None) -> list[dict]:
    today = today or now().date()
    since = today-timedelta(days=cfg.output.recency_days-1)
    groups = defaultdict(dict)
    for row in rows:
        day = date.fromisoformat(row["event_date"])
        if not since<=day<=today:
            continue
        # One source URL contributes once, even across versions/rounds.
        old = groups[row["hash"]].get(row["source_url"])
        if old is None or row["event_date"]>old["event_date"]:
            groups[row["hash"]][row["source_url"]] = row
    ranked = []
    for h,posts in groups.items():
        if len(posts)<cfg.output.high_frequency_min_posts:
            continue
        values = list(posts.values())
        first = values[0]
        ranked.append({"hash":h,"question":first["question"],"q_type":first["q_type"],
            "companies":"、".join(sorted({r["company"] or "未知公司" for r in values})),
            "position_category":"、".join(sorted({CATEGORY_DIRS.get(r["position_category"],"其他") for r in values})),
            "times_seen":len(posts),"last_seen":max(r["event_date"] for r in values),
            "score":sum(2**(-((today-date.fromisoformat(r["event_date"])).days)/cfg.output.half_life_days) for r in values),
            "sources":[{"url":url,"title":r["post_title"]} for url,r in sorted(posts.items())]})
    ranked.sort(key=lambda r:(-r["score"],-r["times_seen"],r["hash"]))
    return ranked[:cfg.output.top_limit]

def _write(path: Path, content: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp = path.with_suffix(path.suffix+".tmp")
    temp.write_text(content,encoding="utf-8")
    os.replace(temp,path)

def render_all(cfg: AppConfig,store) -> list[Path]:
    env = Environment(loader=FileSystemLoader(cfg.resolve(cfg.output.template_dir)),undefined=StrictUndefined,autoescape=False)
    env.filters["md"] = md
    rows = store.questions_for_render()
    month_groups = defaultdict(list)
    for row in rows:
        if row["event_date"] <= now().date().isoformat():
            month_groups[(row["position_category"],row["event_date"][:7])].append(row)
    current = now().strftime("%Y-%m")
    # Always overwrite current empty outputs, so old stale content never masquerades as new results.
    for category in ("java_backend","agent_ai"):
        month_groups.setdefault((category,current),[])
    root = cfg.resolve(cfg.output.output_dir)
    # Regenerate previously generated month files as empty if all their records
    # were removed by review/re-extraction; never leave invalidated counts behind.
    for category,dirname in CATEGORY_DIRS.items():
        for path in (root/dirname).glob("????-??.md"):
            if re.fullmatch(r"\d{4}-\d{2}",path.stem):
                month_groups.setdefault((category,path.stem),[])
    written = []
    for (category,month),items in sorted(month_groups.items()):
        dirname = CATEGORY_DIRS.get(category,"其他")
        path = root/dirname/f"{month}.md"
        _write(path,env.get_template("monthly.md.j2").render(title=f"{dirname} 面经汇总（{month}）",generated_at=now().isoformat(timespec="seconds"),count=len(items),companies=sorted({r["company"] or "未知公司" for r in items}),groups=_group_company_round(items),disclaimer=DISCLAIMER))
        written.append(path)
    path = root/"高频题"/"近30天高频题Top50.md"
    _write(path,env.get_template("top.md.j2").render(title=f"近{cfg.output.recency_days}天高频题 Top{cfg.output.top_limit}",items=rank_questions(rows,cfg),generated_at=now().isoformat(timespec="seconds"),disclaimer=DISCLAIMER,min_posts=cfg.output.high_frequency_min_posts,half_life=cfg.output.half_life_days))
    written.append(path)
    return written

def main() -> int:
    import sys
    from .__main__ import main as cli
    return cli(["render",*sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(main())
