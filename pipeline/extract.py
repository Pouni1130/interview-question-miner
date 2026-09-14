"""DeepSeek-compatible extraction with evidence checks, checkpoints and budgets."""
from __future__ import annotations
import hashlib
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from openai import OpenAI
from .budget import TokenBudget, BudgetPaused
from .config import AppConfig
from .models import ExtractionResult
from .runtime import atomic_json, iso_date, now
from .schema import EXTRACTION_SCHEMA, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

class ExtractFailed(RuntimeError):
    pass

def _parse_json_loose(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$","",text)
    try:
        value = json.loads(text)
        return value if isinstance(value,dict) else None
    except ValueError:
        return None

def _chunk_content(content: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError("chunk_chars 必须为正数")
    chunks,buf = [],""
    for line in content.splitlines(keepends=True):
        while line:
            room = max_chars-len(buf)
            if len(line)<=room:
                buf += line
                break
            if buf:
                chunks.append(buf)
                buf = ""
            else:
                chunks.append(line[:max_chars])
                line = line[max_chars:]
    if buf:
        chunks.append(buf)
    return chunks or [""]

def _merge(base: ExtractionResult, extra: ExtractionResult) -> None:
    """Keep zero confidence; merge only matching rounds, preserving order."""
    if not extra.is_interview_post:
        return
    initialized = getattr(base,"_merged",False)
    if initialized and base.company and extra.company and base.company != extra.company:
        base.confidence = min(base.confidence,.5)
    base.company = base.company or extra.company
    base.department = base.department or extra.department
    base.position = base.position or extra.position
    if base.position_category == "other":
        base.position_category = extra.position_category
    base.summary = base.summary or extra.summary
    base.confidence = min(base.confidence,extra.confidence) if initialized else extra.confidence
    base.is_interview_post = True
    for rnd in extra.rounds:
        target = next((r for r in base.rounds if r["round_name"]==rnd["round_name"] and r.get("date")==rnd.get("date")),None)
        if target:
            target["questions"].extend(rnd["questions"])
        else:
            base.rounds.append(rnd)
    base._merged = True

def _evidence_normalize(text: str) -> str:
    return re.sub(r"\s+","",text)

def sanitize_result(result: ExtractionResult, content: str, cfg: AppConfig) -> list[str]:
    """Unknown dates stay unknown; unsupported question evidence triggers review."""
    notes = []
    normalized = _evidence_normalize(content)
    for rnd in result.rounds:
        rd = rnd.get("date")
        if rd:
            year,month,day = rd.split("-") if iso_date(rd) else ("","","")
            pattern = rf"{year}(?:年|[-/.])0?{int(month)}(?:月|[-/.])0?{int(day)}(?:日|\b)" if year else r"(?!)"
            if not year or not re.search(pattern,normalized) or rd>now().date().isoformat():
                rnd["date"] = None
                notes.append("去除无完整原文依据的日期")
        filtered = []
        for q in rnd["questions"]:
            if any(w in q["question"] for w in cfg.filter.excluded_questions):
                continue
            evidence = _evidence_normalize(q.get("original_text",""))
            if not evidence or evidence not in normalized:
                result.confidence = min(result.confidence,max(0,cfg.llm.confidence_threshold-.01))
                notes.append("题目原文证据无法逐字定位")
            filtered.append(q)
        rnd["questions"] = filtered
    if result.is_interview_post and not any(r["questions"] for r in result.rounds):
        result.confidence = min(result.confidence,max(0,cfg.llm.confidence_threshold-.01))
        notes.append("面经未抽出可核实的问题")
    return list(dict.fromkeys(notes))

class Extractor:
    def __init__(self, cfg: AppConfig):
        self.app = cfg
        self.cfg = cfg.llm
        self.client = OpenAI(base_url=self.cfg.base_url,api_key=self.cfg.api_key,max_retries=0,timeout=self.cfg.timeout) if self.cfg.api_key else None
        self.validator = Draft202012Validator(EXTRACTION_SCHEMA,format_checker=FormatChecker())
        self.budget = TokenBudget(cfg.resolve(cfg.output.db_path).parent/"usage.json",self.cfg.daily_token_budget)
        self.checkpoints = cfg.resolve(cfg.output.db_path).parent/"checkpoints"

    def close(self):
        if self.client:
            self.client.close()

    def _chat(self, prompt: str) -> tuple[str,int | None]:
        response = self.client.chat.completions.create(model=self.cfg.model,
            messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],
            temperature=self.cfg.temperature,max_tokens=self.cfg.max_tokens,response_format={"type":"json_object"})
        tokens = response.usage.total_tokens if response.usage else None
        return response.choices[0].message.content or "",tokens

    def _extract_with_retry(self, prompt: str, post_id: str = "standalone", chunk: int = 0) -> ExtractionResult:
        errors = []
        for attempt in range(self.cfg.max_retries+1):
            user = prompt + ("\n上次校验失败，请修正 JSON："+"; ".join(errors) if errors else "")
            # UTF-8 bytes upper-bound normal text tokenization, plus a generous
            # protocol overhead; reserve output too. No SDK hidden retries.
            input_bound = len((SYSTEM_PROMPT+user).encode("utf-8"))+256
            reserve = input_bound+self.cfg.max_tokens
            if reserve > self.cfg.context_tokens:
                raise ExtractFailed("本块超过配置的上下文预算；减小 chunk_chars")
            call_id = self.budget.reserve(reserve,post_id,chunk,attempt)
            try:
                text,tokens = self._chat(user)
            except Exception as exc:
                self.budget.settle(call_id,None)
                logger.warning("LLM 调用失败 post=%s chunk=%d attempt=%d type=%s；未知用量保留预算",post_id,chunk,attempt,type(exc).__name__)
                errors = ["API调用失败，请按Schema返回JSON"]
                continue
            self.budget.settle(call_id,tokens)
            logger.info("Token post=%s chunk=%d attempt=%d used=%s today=%d",post_id,chunk,attempt,tokens,self.budget.used_today)
            data = _parse_json_loose(text)
            if data is None:
                errors = ["输出不是合法 JSON"]
                continue
            failures = list(self.validator.iter_errors(data))
            if failures:
                errors = [f"{'/'.join(map(str,e.path))}: {e.message[:200]}" for e in failures[:5]]
                continue
            return ExtractionResult(post_url="",**data)
        raise ExtractFailed("Schema/API 重试耗尽："+"; ".join(errors))

    def extract(self,title: str,content: str,post_id: str = "",publish_time: str = "") -> ExtractionResult:
        if not self.client:
            raise BudgetPaused("未配置 DeepSeek API key")
        key = hashlib.sha256(json.dumps([title,content,self.cfg.model,self.cfg.base_url,self.cfg.chunk_chars,self.cfg.max_tokens,SYSTEM_PROMPT,EXTRACTION_SCHEMA],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        checkpoint = self.checkpoints/f"{post_id or key}.json"
        cache = {"key":key,"chunks":{}}
        if checkpoint.exists():
            try:
                old = json.loads(checkpoint.read_text(encoding="utf-8"))
                if old.get("key")==key:
                    cache = old
            except (ValueError,OSError):
                pass
        merged = ExtractionResult(post_url="",is_interview_post=False)
        chunks = _chunk_content(content,self.cfg.chunk_chars)
        preceding = ""
        for i,chunk in enumerate(chunks):
            cached = cache["chunks"].get(str(i))
            if cached:
                result = ExtractionResult(**cached)
            else:
                prompt = f"帖子标题：{title}\n发布日期（不能代替面试日期）：{publish_time}\n前文分场标题上下文（只用于归属，不从此抽题）：{preceding[-600:]}\n正文第{i+1}/{len(chunks)}块：\n{chunk}"
                result = self._extract_with_retry(prompt,post_id or key,i)
                cache["chunks"][str(i)] = asdict(result)
                atomic_json(checkpoint,cache)
            _merge(merged,result)
            headings = [line.strip() for line in chunk.splitlines() if len(line.strip())<=100 and re.search(r"面经\s*\d+|[一二三四五终]面|面试时间|面试岗位|面试公司",line)]
            if headings:
                preceding = "\n".join(headings[-6:])
        return merged

def coarse_filter_pass(cfg: AppConfig,store) -> tuple[int,int]:
    from .filter import passes_coarse_filter
    passed = failed = 0
    for row in store.posts_by_status("raw"):
        content = store.get_post_content(row["fingerprint"])
        if not content:
            store.set_status(row["fingerprint"],"extract_failed","raw_missing")
            failed += 1
            continue
        ok,_ = passes_coarse_filter(row["title"] or "",content,cfg.filter.interview_words,cfg.filter.position_words)
        store.set_status(row["fingerprint"],"to_extract" if ok else "filtered")
        passed += int(ok)
        failed += int(not ok)
    logger.info("粗过滤: 通过 %d，拦截/缺失 %d",passed,failed)
    return passed,failed

def pending_path(cfg: AppConfig, fp: str) -> Path:
    return cfg.resolve(cfg.output.pending_dir)/f"{fp}.json"

def extract_pass(cfg: AppConfig,store,limit: int | None = None) -> int:
    extractor = Extractor(cfg)
    processed = 0
    try:
        for row in store.posts_by_status("to_extract"):
            if limit is not None and processed>=limit:
                break
            fp,url = row["fingerprint"],row["url"]
            try:
                content = store.get_post_content(fp)
                if not content:
                    raise ExtractFailed("raw_missing")
                result = extractor.extract(row["title"] or "",content,fp,row["publish_time"])
                result.post_url = url
                notes = sanitize_result(result,content,cfg)
                if not result.is_interview_post:
                    store.set_status(fp,"discarded")
                elif result.confidence<cfg.llm.confidence_threshold:
                    atomic_json(pending_path(cfg,fp),{"post_fingerprint":fp,"post_url":url,"notes":notes,"result":asdict(result)})
                    store.set_status(fp,"pending_review","; ".join(notes) or "low_confidence")
                else:
                    count = store.save_extraction(fp,result,cfg.dedup)
                    logger.info("抽取完成 %s: %d 条来源记录",row["title"],count)
                processed += 1
            except BudgetPaused as exc:
                logger.warning("暂停抽取，保留分块进度：%s",exc)
                break
            except Exception as exc:
                store.set_status(fp,"extract_failed",str(exc)[:500] if isinstance(exc,ExtractFailed) else type(exc).__name__)
                logger.error("抽取失败 post=%s: %s",fp,type(exc).__name__)
                processed += 1
    finally:
        extractor.close()
    return processed

def apply_review(cfg: AppConfig,store,path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    fp = data["post_fingerprint"]
    row = store.get_post(fp)
    if row is None or row["status"] != "pending_review":
        raise ValueError("帖子不是待复核状态")
    result_data = dict(data["result"])
    result_data.pop("post_url",None)
    Draft202012Validator(EXTRACTION_SCHEMA,format_checker=FormatChecker()).validate(result_data)
    result = ExtractionResult(post_url=row["url"],**result_data)
    notes = sanitize_result(result,store.get_post_content(fp),cfg)
    if result.is_interview_post and (notes or result.confidence<cfg.llm.confidence_threshold):
        raise ValueError("复核未通过：请修正原文证据、日期和置信度。"+"; ".join(notes))
    if result.is_interview_post:
        store.save_extraction(fp,result,cfg.dedup)
    else:
        store.set_status(fp,"discarded")
    atomic_json(path.with_suffix(".reviewed.json"),data | {"reviewed_at":now().isoformat()})

def revalidate(cfg: AppConfig,store) -> int:
    """Apply current deterministic quality rules to saved results, without LLM calls."""
    processed=0
    for row in store.posts_by_status("extracted"):
        if not row["extraction_json"]:
            continue
        result=ExtractionResult(**json.loads(row["extraction_json"]))
        fp=row["fingerprint"]
        notes=sanitize_result(result,store.get_post_content(fp),cfg)
        if result.confidence<cfg.llm.confidence_threshold:
            atomic_json(pending_path(cfg,fp),{"post_fingerprint":fp,"post_url":row["url"],"notes":notes,"result":asdict(result)})
            store.set_status(fp,"pending_review","; ".join(notes))
        else:
            store.save_extraction(fp,result,cfg.dedup)
        processed+=1
    return processed

def main() -> int:
    import sys
    from .__main__ import main as cli
    return cli(["extract",*sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(main())
