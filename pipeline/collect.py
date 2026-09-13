"""采集层:牛客网公开面经搜索与详情抓取。

2026-09 实测:牛客搜索页已改为 AJAX 动态加载(SSR 搜索页失效),
改用其公开搜索接口 gw-c.nowcoder.com/api/sparta/pc/search:
- contentType 250(POST,讨论区帖):搜索结果只给摘要,
  详情走 https://www.nowcoder.com/discuss/{contentID}(SSR 全文,
  选择器参考 InterviewRadar:div.nc-slate-editor-content + createTime 正则);
- contentType 74(MOMENTS,动态帖):正文短,搜索结果即全文,直接入库。

采集纪律:低频(request_interval 秒)、只抓公开页、绝不绕过登录/验证码、
失败指数退避重试最多 max_retries 次。原始数据一经落盘永不修改。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .config import AppConfig

logger = logging.getLogger(__name__)

NOWCODER_BASE = "https://www.nowcoder.com"
SEARCH_API = "https://gw-c.nowcoder.com/api/sparta/pc/search"
CREATE_TIME_RE = re.compile(r'"createTime"\s*:\s*(\d{10,13})')
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def post_id_from_url(url: str) -> str:
    """从 /discuss/123 或 /feed/main/detail/xxx 提取帖子唯一 ID。"""
    m = re.search(r"/discuss/(\d+)", url) or re.search(r"/feed/main/detail/([\w-]+)", url)
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-40:]


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
    except (ValueError, OSError):
        return None


class NowcoderCollector:
    """牛客采集器:搜索接口发现 + 详情抓取,带限频与退避重试。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg.collect
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            }
        )
        if self.cfg.cookie:
            self.session.headers["Cookie"] = self.cfg.cookie

    # ---------- HTTP 基础 ----------

    def _sleep(self) -> None:
        time.sleep(self.cfg.request_interval)

    def _get(self, url: str) -> str:
        """带限频与指数退避的 GET(HTML 页)。"""
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            self._sleep()
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code in (403, 412, 429):
                    wait = 2**attempt * 30
                    logger.warning("HTTP %s 于 %s,退避 %ss", resp.status_code, url, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:
                last_exc = exc
                wait = 2**attempt * 5
                logger.warning("请求失败(%s/%s): %s,退避 %ss", attempt + 1, self.cfg.max_retries, exc, wait)
                time.sleep(wait)
        raise ConnectionError(f"重试耗尽: {url}") from last_exc

    # ---------- 搜索(公开 AJAX 接口) ----------

    def search_posts(self, keyword: str, max_pages: int) -> list[dict]:
        """搜索一个关键词,返回候选列表:
        [{url, post_id, kind('post'|'moment'), title, snippet, author, publish_time}]

        接口单词 total 上限 400,分页大小 20。
        """
        candidates: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            body = {
                "query": keyword,
                "type": "post",
                "page": page,
                "tag": [],
                "order": "",
                "pageSize": 20,
            }
            try:
                self._sleep()
                resp = self.session.post(SEARCH_API, json=body, timeout=20)
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.error("搜索接口失败[%s 第%d页]: %s", keyword, page, exc)
                break
            if not payload.get("success"):
                logger.error("搜索接口返回失败[%s 第%d页]: %s", keyword, page, payload.get("msg"))
                break
            data = payload.get("data") or {}
            records = data.get("records") or []
            for rec in records:
                cand = _parse_search_record(rec)
                if cand and cand["url"] not in seen:
                    seen.add(cand["url"])
                    candidates.append(cand)
            if page >= (data.get("totalPage") or 1):
                break
        return candidates

    # ---------- 详情 ----------

    def fetch_post_full(self, cand: dict) -> str | None:
        """抓取帖子全文:POST 类型抓 discuss SSR 页;MOMENTS 直接用摘要。"""
        if cand["kind"] == "moment":
            return cand["snippet"]
        try:
            html = self._get(cand["url"])
        except ConnectionError as exc:
            logger.error("跳过不可达帖子 %s: %s", cand["url"], exc)
            return None
        soup = BeautifulSoup(html, "lxml")
        content_el = soup.select_one("div.nc-slate-editor-content") or soup.select_one(
            ".post-topic-des"
        )
        content = content_el.get_text("\n", strip=True) if content_el else ""
        if not content:
            logger.warning("正文解析为空(选择器可能漂移): %s", cand["url"])
            return cand["snippet"] or None
        return content


def _parse_search_record(rec: dict) -> dict | None:
    """把搜索接口的一条记录解析为候选 dict;解析不出 URL 时返回 None。"""
    extra = rec.get("extraInfo") or {}
    kind = extra.get("contentType_var")
    content_data = rec.get("contentData") or {}
    moment_data = rec.get("momentData") or {}
    if kind == "POST" and content_data:
        content_id = extra.get("contentID_var") or str(content_data.get("id") or "")
        if not content_id.isdigit():
            return None
        url = f"{NOWCODER_BASE}/discuss/{content_id}"
        title = content_data.get("title") or ""
        snippet = content_data.get("content") or ""
        author = (rec.get("userBrief") or {}).get("nickname") or ""
        publish_time = _ms_to_iso(content_data.get("createTime"))
        return {
            "url": url,
            "post_id": content_id,
            "kind": "post",
            "title": _clean(title),
            "snippet": snippet,
            "author": author,
            "publish_time": publish_time,
        }
    if kind == "MOMENTS" and moment_data:
        uuid = moment_data.get("uuid") or ""
        if not uuid:
            return None
        url = f"{NOWCODER_BASE}/feed/main/detail/{uuid}"
        return {
            "url": url,
            "post_id": uuid,
            "kind": "moment",
            "title": _clean(moment_data.get("title") or ""),
            "snippet": moment_data.get("content") or "",
            "author": (rec.get("userBrief") or {}).get("nickname") or "",
            "publish_time": _ms_to_iso(moment_data.get("createdAt")),
        }
    return None


def _clean(text: str) -> str:
    return unescape(re.sub(r"\s+", " ", text or "")).strip()


def _extract_publish_time(html: str) -> str | None:
    """从 discuss 详情页内嵌 JS 提取 createTime(参考 InterviewRadar)。"""
    m = CREATE_TIME_RE.search(html)
    if not m:
        return None
    ts = int(m.group(1))
    if ts > 10_000_000_000:
        ts //= 1000
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (ValueError, OSError):
        return None


# ---------- 落盘 ----------


def save_raw(raw_dir: Path, post: "RawPost") -> Path:
    """原始数据落地 raw/{source}/{yyyy-mm-dd}/{post_id}.json,永不覆盖已有文件。"""
    import json

    day = datetime.now().strftime("%Y-%m-%d")
    out_dir = raw_dir / post.source / day
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{post.post_id}.json"
    if out.exists():
        return out
    out.write_text(json.dumps(post.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out


class RawPost:
    """原始帖:落盘与入库的统一载体。"""

    def __init__(self, url: str, post_id: str, title: str, author: str,
                 publish_time: str | None, crawl_time: str, content_raw: str,
                 source: str = "nowcoder"):
        self.url = url
        self.post_id = post_id
        self.title = title
        self.author = author
        self.publish_time = publish_time
        self.crawl_time = crawl_time
        self.content_raw = content_raw
        self.source = source

    def to_dict(self) -> dict:
        return {
            "url": self.url, "post_id": self.post_id, "title": self.title,
            "author": self.author, "publish_time": self.publish_time,
            "crawl_time": self.crawl_time, "content_raw": self.content_raw,
            "source": self.source,
        }


def collect_incremental(cfg: AppConfig) -> int:
    """执行增量采集:搜索 → 过滤已见 → 抓全文 → 落盘入库。返回新增帖数。"""
    from .store import Store

    store = Store(cfg.resolve(cfg.output.db_path))
    collector = NowcoderCollector(cfg)
    fetched = 0

    for kw in cfg.collect.keywords:
        if fetched >= cfg.collect.daily_limit:
            logger.info("已达每日上限 %d,停止采集", cfg.collect.daily_limit)
            break
        cands = collector.search_posts(kw, cfg.collect.max_pages_per_keyword)
        logger.info("关键词[%s] 发现 %d 条候选", kw, len(cands))
        for cand in cands:
            if fetched >= cfg.collect.daily_limit:
                break
            if store.has_post(cand["url"]):
                continue
            content = collector.fetch_post_full(cand)
            if not content:
                continue
            post = RawPost(
                url=cand["url"],
                post_id=cand["post_id"],
                title=cand["title"],
                author=cand["author"],
                publish_time=cand["publish_time"],
                crawl_time=datetime.now().isoformat(timespec="seconds"),
                content_raw=f"{cand['title']}\n\n{content}" if content else cand["title"],
            )
            save_raw(cfg.resolve(cfg.output.raw_dir), post)
            store.upsert_post(post, status="raw")
            fetched += 1
            logger.debug("已采集(%d): %s", fetched, post.title)

    logger.info("本次采集完成:新增 %d 帖", fetched)
    return fetched


def main(config_path: str | None = None) -> int:
    """独立入口:python -m pipeline.collect [--config path]"""
    import argparse

    from .config import load_config, setup_logging

    parser = argparse.ArgumentParser(description="牛客面经增量采集")
    parser.add_argument("--config", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config(config_path or args.config)
    setup_logging(cfg.resolve(cfg.output.log_dir), args.verbose)
    collect_incremental(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
