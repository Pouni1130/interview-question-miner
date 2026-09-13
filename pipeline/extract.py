"""LLM 抽取层(项目核心):面经帖 → 严格 JSON 结构化。

- OpenAI 兼容协议接入(DeepSeek/GLM 等),key/model 全部来自 config.yaml;
- 输出通过 JSON Schema 校验,失败附带错误信息重试最多 2 次;
- is_interview_post=false 的帖子直接丢弃;
- confidence < 0.6 的结果写入 pending/ 目录等人工复核;
- 超长帖按段落分块抽取后合并;
- 每日 Token 预算超限即暂停(状态记录在 data/usage.json)。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from jsonschema import Draft202012Validator
from openai import OpenAI

from .config import AppConfig
from .models import ExtractionResult

logger = logging.getLogger(__name__)

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "required": ["company", "position", "position_category", "rounds",
                 "summary", "confidence", "is_interview_post"],
    "properties": {
        "company": {"type": "string"},
        "department": {"type": "string"},
        "position": {"type": "string"},
        "position_category": {"enum": ["java_backend", "agent_ai", "other"]},
        "rounds": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["round_name", "questions"],
                "properties": {
                    "round_name": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["question", "type"],
                            "properties": {
                                "question": {"type": "string"},
                                "type": {
                                    "enum": ["场景设计", "八股", "项目追问",
                                             "手撕代码", "开放问答"]
                                },
                                "follow_ups": {
                                    "type": "array", "items": {"type": "string"}
                                },
                                "original_text": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "is_interview_post": {"type": "boolean"},
    },
}

SYSTEM_PROMPT = """你是面经结构化助手。输入是一篇互联网求职面试经验帖的原始文本。
你的任务:把它抽取为严格的 JSON(只输出 JSON,不要任何多余文字或代码块标记),schema 如下:
{
  "company": "公司名,帖中未提则空字符串",
  "department": "部门/业务线,未提则空字符串",
  "position": "岗位名(如 Java后端开发、AI应用开发)",
  "position_category": "java_backend | agent_ai | other 三选一:
      内容重心是 Java/Spring/JVM/MySQL/Redis/MQ/微服务 等后端技术 → java_backend;
      内容重心是 Agent/RAG/LLM应用/Function Call/Prompt/微调 等 → agent_ai;
      两者都沾按篇幅重心判,都不沾 → other",
  "rounds": [{"round_name": "一面/二面/三面/HR面 等,未区分则用 '未标注轮次'",
              "date": "轮次日期 YYYY-MM-DD 或 null",
              "questions": [{"question": "被问到的问题(去掉寒暄,保留技术实体与条件)",
                             "type": "场景设计|八股|项目追问|手撕代码|开放问答 五选一",
                             "follow_ups": ["面试官由浅入深的追问,按顺序"],
                             "original_text": "原文中对应的片段(可截取,不超过200字)"}]}],
  "summary": "一句话总结整体难度与考察重点",
  "confidence": 0~1 的抽取置信度,
  "is_interview_post": true/false —— 纯八股分享/广告/招聘启事/非面试内容必须为 false
}
要求:只抽"实际面试中被问到的问题",不要把楼主自己的总结当题目;追问链保持原始顺序;
正文无关内容(进度贴、吐槽、寒暄)一律不抽;题目不确定时调低 confidence。"""


class TokenBudget:
    """每日 Token 用量记录与预算控制(持久化到 data/usage.json)。"""

    def __init__(self, usage_path: Path, daily_budget: int):
        self.path = usage_path
        self.daily_budget = daily_budget
        self.used_today = 0
        self._load()

    def _load(self) -> None:
        today = date.today().isoformat()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("date") == today:
                    self.used_today = int(data.get("used", 0))
            except (OSError, ValueError):
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"date": date.today().isoformat(), "used": self.used_today},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def can_spend(self) -> bool:
        return self.used_today < self.daily_budget

    def add(self, tokens: int) -> None:
        self.used_today += tokens
        self._save()


class Extractor:
    """调用 LLM 完成单帖结构化抽取。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg.llm
        self.client: OpenAI | None = None
        if cfg.llm.api_key:
            self.client = OpenAI(base_url=cfg.llm.base_url, api_key=cfg.llm.api_key)
        else:
            logger.warning("未配置 llm.api_key,抽取功能不可用(采集/过滤/渲染不受影响)")
        self.validator = Draft202012Validator(EXTRACTION_SCHEMA)
        self.budget = TokenBudget(
            cfg.resolve(cfg.output.db_path).parent / "usage.json",
            cfg.llm.daily_token_budget,
        )

    def _chat(self, user_prompt: str) -> tuple[str, int]:
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=8192,  # 长帖抽取输出大,显式给满上限防截断
            response_format={"type": "json_object"},
        )
        tokens = (resp.usage.prompt_tokens + resp.usage.completion_tokens) if resp.usage else 0
        return resp.choices[0].message.content or "", tokens

    def extract(self, title: str, content: str) -> ExtractionResult | None:
        """抽取单帖;schema 校验失败自动重试(最多 2 次,附带错误信息)。"""
        if not self.cfg.api_key:
            logger.warning("未配置 LLM api_key,跳过抽取(帖子保留 to_extract 状态,下次运行重试)")
            return None
        if not self.budget.can_spend():
            logger.warning("已达每日 Token 预算上限(%d),暂停抽取", self.cfg.daily_token_budget)
            return None

        chunks = _chunk_content(content, self.cfg.chunk_chars)
        merged = ExtractionResult(post_url="")
        for i, chunk in enumerate(chunks):
            user_prompt = (
                f"帖子标题:{title}\n\n帖子正文(第 {i + 1}/{len(chunks)} 块):\n{chunk}"
            )
            result = self._extract_with_retry(user_prompt)
            if result is None:
                return None
            if not result.is_interview_post:
                return result
            _merge(merged, result)
        merged.post_url = ""
        return merged

    def _extract_with_retry(self, user_prompt: str) -> ExtractionResult | None:
        last_errors: list[str] = []
        for attempt in range(3):  # 首次 + 2 次重试
            prompt = user_prompt
            if last_errors:
                prompt += f"\n\n注意:你上次的输出未通过校验,错误:{'; '.join(last_errors)}。请修正后重新只输出 JSON。"
            try:
                text, tokens = self._chat(prompt)
            except Exception as exc:
                logger.error("LLM 调用失败(%d/3): %s", attempt + 1, exc)
                last_errors = [f"API 调用异常: {exc}"]
                continue
            self.budget.add(tokens)
            logger.debug("本帖 Token 用量: %d,今日累计: %d", tokens, self.budget.used_today)
            data = _parse_json_loose(text)
            if data is None:
                last_errors = ["输出不是合法 JSON"]
                continue
            errors = sorted(self.validator.iter_errors(data), key=lambda e: str(e.path))
            if errors:
                last_errors = [
                    f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]
                ]
                continue
            return ExtractionResult(
                post_url="",
                company=data.get("company", ""),
                department=data.get("department", ""),
                position=data.get("position", ""),
                position_category=data.get("position_category", "other"),
                rounds=data.get("rounds", []),
                summary=data.get("summary", ""),
                confidence=float(data.get("confidence", 0)),
                is_interview_post=bool(data.get("is_interview_post", True)),
            )
        logger.error("抽取重试耗尽:%s", "; ".join(last_errors))
        return None


def _chunk_content(content: str, max_chars: int) -> list[str]:
    """按段落聚合分块;单个超长段落做硬切,保证每块不超过 max_chars。"""
    if len(content) <= max_chars:
        return [content]
    chunks: list[str] = []
    buf = ""
    for para in content.split("\n"):
        # 硬切超长段落
        pieces = [para[i : i + max_chars] for i in range(0, len(para), max_chars)] or [""]
        for piece in pieces:
            if len(buf) + len(piece) > max_chars and buf:
                chunks.append(buf)
                buf = ""
            buf += piece + "\n"
    if buf:
        chunks.append(buf)
    return chunks


def _parse_json_loose(text: str) -> dict | None:
    """容忍模型输出 ```json 包裹的情况。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _merge(base: ExtractionResult, extra: ExtractionResult) -> None:
    """分块抽取结果合并:公司/岗位取第一个非空值,轮次拼接。"""
    base.company = base.company or extra.company
    base.department = base.department or extra.department
    base.position = base.position or extra.position
    if base.position_category == "other":
        base.position_category = extra.position_category
    base.rounds.extend(extra.rounds)
    base.summary = base.summary or extra.summary
    base.confidence = min(base.confidence, extra.confidence) if base.confidence else extra.confidence
    base.is_interview_post = base.is_interview_post and extra.is_interview_post


# ---------- 流水线步骤(供 python -m pipeline.extract 与 run 共用) ----------


def coarse_filter_pass(cfg: AppConfig, store: Store) -> tuple[int, int]:
    """对 status='raw' 的帖子做粗过滤:通过 → 'to_extract',不通过 → 'filtered'。"""
    from .filter import passes_coarse_filter

    passed = failed = 0
    for row in store.posts_by_status("raw"):
        content = store.get_post_content(row["url"])
        ok, reason = passes_coarse_filter(
            row["title"] or "",
            content,
            cfg.filter.interview_words,
            cfg.filter.position_words,
        )
        if ok:
            store.set_status(row["url"], "to_extract")
            passed += 1
            logger.debug("通过粗过滤: %s (%s)", row["title"], reason)
        else:
            store.set_status(row["url"], "filtered")
            failed += 1
            logger.debug("粗过滤拦截: %s (%s)", row["title"], reason)
    logger.info("粗过滤完成:通过 %d,拦截 %d", passed, failed)
    return passed, failed


def extract_pass(cfg: AppConfig, store: Store) -> int:
    """对 status='to_extract' 的帖子做 LLM 抽取并落库。"""
    import hashlib

    from .dedup import is_duplicate, normalize_question, question_hash
    from .models import QuestionRecord

    extractor = Extractor(cfg)
    pending_dir = cfg.resolve(cfg.output.pending_dir)
    pending_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    processed = 0

    for row in store.posts_by_status("to_extract"):
        url = row["url"]
        content = store.get_post_content(url)
        if not content:
            logger.warning("正文缺失,无法抽取: %s", url)
            continue
        try:
            result = extractor.extract(row["title"] or "", content)
        except Exception as exc:
            logger.error("抽取异常(不阻塞流水线): %s: %s", url, exc)
            continue
        if result is None:
            continue  # 无 key / 预算超限:保持 to_extract,下次重试
        if not result.is_interview_post:
            store.set_status(url, "discarded")
            logger.info("非面经帖,丢弃: %s", row["title"])
            processed += 1
            continue
        if result.confidence < 0.6:
            slug = hashlib.md5(url.encode()).hexdigest()[:12]
            (pending_dir / f"{slug}.json").write_text(
                json.dumps(result.__dict__ | {"post_url": url}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            store.set_status(url, "pending_review")
            logger.info("低置信度(%.2f),转人工复核: %s", result.confidence, row["title"])
            processed += 1
            continue

        existing_hashes = store.all_recent_hashes(days=60)
        seen_pairs = [(h, q) for h, q in existing_hashes]
        inserted = 0
        for rnd in result.rounds:
            for q in rnd.get("questions", []):
                raw_q = q.get("question", "").strip()
                if not raw_q:
                    continue
                qhash = question_hash(raw_q)
                norm = normalize_question(raw_q)
                dup_hash = is_duplicate(norm, seen_pairs)
                if dup_hash:
                    store.bump_question(dup_hash, url, today)
                    continue
                record = QuestionRecord(
                    hash=qhash,
                    post_url=url,
                    company=result.company,
                    position=result.position,
                    position_category=result.position_category,
                    round_name=rnd.get("round_name", ""),
                    round_date=rnd.get("date"),
                    question=raw_q,
                    q_type=q.get("type", ""),
                    follow_ups=json.dumps(q.get("follow_ups", []), ensure_ascii=False),
                    original_text=q.get("original_text", ""),
                    first_seen=today,
                    last_seen=today,
                )
                store.insert_question(record)
                seen_pairs.append((qhash, raw_q))
                inserted += 1
        store.update_extraction(
            url,
            result.company,
            result.position,
            result.position_category,
            result.summary,
            result.confidence,
        )
        logger.info("抽取入库: %s (新题 %d)", row["title"], inserted)
        processed += 1
    return processed


def main(config_path: str | None = None) -> int:
    """独立入口:python -m pipeline.extract [--config path]"""
    import argparse

    from .config import load_config, setup_logging
    from .store import Store

    parser = argparse.ArgumentParser(description="LLM 抽取(含前置粗过滤)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config(config_path or args.config)
    setup_logging(cfg.resolve(cfg.output.log_dir), args.verbose)
    store = Store(cfg.resolve(cfg.output.db_path))
    store.bind_raw_root(cfg.resolve(cfg.output.raw_dir))
    coarse_filter_pass(cfg, store)
    n = extract_pass(cfg, store)
    logger.info("抽取完成:处理 %d 帖", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
