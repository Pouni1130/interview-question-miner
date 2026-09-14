"""Public HTML/RSS/sitemap collection; Nowcoder first, CSDN and Juejin supplemental."""
from __future__ import annotations
import hashlib
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from .config import AppConfig
from .http import PublicHTTP, SkipPage, SourceUnavailable
from .runtime import now, LOCAL_TZ, iso_date

logger = logging.getLogger(__name__)
ARTICLE_PATTERNS = {
    "nowcoder": re.compile(r"^/(?:discuss/\d+|feed/main/detail/[\w-]+)$"),
    "csdn": re.compile(r"^/[^/]+/article/details/\d+$"),
    "juejin": re.compile(r"^/post/\d+$"),
}
CONTENT_SELECTORS = {
    "nowcoder": ["div.nc-slate-editor-content", ".post-topic-des", ".feed-content-text"],
    "csdn": ["#content_views"],
    "juejin": [".article-content", ".markdown-body"],
}

@dataclass
class RawPost:
    url: str
    post_id: str
    title: str
    author: str
    publish_time: str | None
    crawl_time: str
    content_raw: str
    source: str = "nowcoder"

    def to_dict(self) -> dict:
        return asdict(self)

def canonical_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme,p.netloc,p.path.rstrip("/"),"",""))

def post_id_from_url(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/",1)[-1]

def save_raw(raw_dir: Path, post: RawPost) -> Path:
    """Immutable snapshots; a different publication timestamp gets a suffix."""
    from .store import fingerprint
    day = iso_date(post.crawl_time) or now().date().isoformat()
    path = raw_dir / post.source / day / f"{post.post_id}.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("url") == post.url and old.get("publish_time") == post.publish_time:
            return path
        path = path.with_name(f"{post.post_id}-{fingerprint(post.url,post.publish_time or '')[:12]}.json")
    if not path.exists():
        # Complete temp write followed by rename; CLI holds the process lock.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(post.to_dict(),ensure_ascii=False,indent=2),encoding="utf-8")
        tmp.rename(path)
    return path

def _published(soup: BeautifulSoup, html: str, source: str) -> str | None:
    for selector in ('meta[property="article:published_time"]','meta[itemprop="datePublished"]','meta[name="publishdate"]','time[datetime]'):
        tag = soup.select_one(selector)
        if tag:
            value = tag.get("content") or tag.get("datetime")
            if iso_date(value):
                return str(value)
    # Explicit structured timestamps, not a sitemap's lastmod or crawl time.
    patterns = [r'"datePublished"\s*:\s*"([^"\n]+)"']
    if source == "nowcoder":
        match = re.search(r'"createTime"\s*:\s*(\d{10,13})',html)
        if match:
            stamp = int(match.group(1))
            if stamp > 10000000000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp,LOCAL_TZ).isoformat(timespec="seconds")
    for pattern in patterns:
        match = re.search(pattern,html)
        if match and iso_date(match.group(1)):
            return match.group(1)
    for selector in ([".time", ".blog-article-content .article-info-box .time"] if source == "csdn" else [".article-meta-box time", ".article-info .time"]):
        tag = soup.select_one(selector)
        match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})",tag.get_text() if tag else "")
        if match:
            value = f"{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}"
            if iso_date(value):
                return value
    return None

def parse_article(url: str, html: str, source: str) -> RawPost:
    """Read only rendered public article content, never hidden paid payloads."""
    soup = BeautifulSoup(html,"lxml")
    page_text = soup.get_text(" ",strip=True)
    pay_markers = ("付费后可阅读", "付费解锁", "购买后阅读", "购买专栏解锁", "开通VIP后", "订阅后可阅读", "会员专享文章", "登录后继续阅读", "登录后查看全文", "登录后可查看", "扫码登录后", "VIP专享文章")
    if any(marker.lower() in page_text.lower() for marker in pay_markers) or soup.select_one(".blog-tags-box .isblogvip, .hide-article-box, .article-paywall, [data-paywall='true']"):
        raise SkipPage("付费/会员/登录限制，整篇跳过")
    if re.search(r'"(?:is_pay|is_paid|isCharge|isVip|need_pay)"\s*:\s*(?:true|1)\b',html,re.I):
        raise SkipPage("付费内容标记，整篇跳过")
    content_el = next((soup.select_one(selector) for selector in CONTENT_SELECTORS[source] if soup.select_one(selector)),None)
    if not content_el:
        raise SkipPage("公开正文缺失；不以搜索摘要代替全文")
    for tag in content_el.select("script,style,nav,.advertisement,.recommend-box"):
        tag.decompose()
    content = content_el.get_text("\n",strip=True)
    if len(content) < 30:
        raise SkipPage("正文过短或不完整")
    h1 = soup.select_one("h1")
    meta = soup.select_one('meta[property="og:title"]')
    title = h1.get_text(" ",strip=True) if h1 else (meta.get("content","") if meta else "")
    author_tag = soup.select_one('meta[name="author"]')
    author = author_tag.get("content","") if author_tag else ""
    if not author:
        author_el = soup.select_one(".user-name, #uid, .author-name, .name")
        author = author_el.get_text(" ",strip=True) if author_el else ""
    published = _published(soup,html,source)
    if not published:
        raise SkipPage("缺少可靠发布日期；不能按采集时间假定为新帖")
    return RawPost(canonical_url(url),post_id_from_url(url),title,author,published,now().isoformat(timespec="seconds"),content,source)

class PublicCollector:
    def __init__(self, cfg: AppConfig, source: str):
        self.app = cfg
        self.source = source
        self.cfg = cfg.sources[source]
        self.http = PublicHTTP(cfg.collect,self.cfg.hosts)

    def discover(self) -> list[str]:
        """Bounded discovery from allowed lists, RSS/Atom and sitemaps."""
        candidates = list(self.cfg.seed_urls)
        relevance = {}
        queue = list(self.cfg.discovery_urls)
        visited = set()
        for _ in range(self.cfg.max_discovery_pages):
            if not queue:
                break
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                response = self.http.get(url)
            except SourceUnavailable:
                raise
            except SkipPage as exc:
                logger.warning("[%s] 发现页跳过 %s: %s",self.source,url,exc)
                continue
            is_xml = response.text.lstrip().startswith("<?xml") or "xml" in response.headers.get("Content-Type", "")
            soup = BeautifulSoup(response.text,"xml" if is_xml else "lxml")
            links = []
            if is_xml:
                # Reversing URL entries prefers recent entries when sorted by ID;
                # publication dates are always checked on the actual article.
                for tag in soup.select("loc, item > link, entry > link"):
                    links.append((tag.get("href") or tag.get_text(strip=True),""))
            else:
                for tag in soup.select("a[href]"):
                    links.append((urljoin(url,tag["href"]),tag.get_text(" ",strip=True)))
                if not any(ARTICLE_PATTERNS[self.source].match(urlsplit(link).path.rstrip("/")) for link,_ in links):
                    logger.warning("[%s] 公开列表没有文章链接（可能仅为动态页面）；请配置允许的 RSS/站点地图或公开文章 seed_urls：%s",self.source,url)
            for link,title in links:
                p = urlsplit(link)
                if p.hostname not in self.cfg.hosts or p.scheme != "https":
                    continue
                if p.path.endswith(".xml") and link not in visited:
                    queue.append(link)
                elif ARTICLE_PATTERNS[self.source].match(p.path.rstrip("/")):
                    # Title gate before spending requests; unknown titles (RSS,
                    # sitemap or seeds) go through the full-content coarse filter.
                    if title and self.app.filter.interview_words and not any(w.lower() in title.lower() for w in self.app.filter.interview_words):
                        continue
                    candidates.append(canonical_url(link))
                    normalized = title.lower()
                    score = sum(w.lower() in normalized for w in self.app.filter.position_words)
                    score += 2 * sum(w.lower() in normalized for w in self.app.collect.keywords)
                    key = canonical_url(link)
                    relevance[key] = max(relevance.get(key,0),score)
        candidates = list(dict.fromkeys(candidates))
        seeds = set(self.cfg.seed_urls)
        candidates.sort(key=lambda u: (u in seeds,relevance.get(u,0),post_id_from_url(u)),reverse=True)
        return candidates[:self.app.collect.max_candidates_per_source]

    def fetch(self, url: str) -> RawPost:
        if not ARTICLE_PATTERNS[self.source].match(urlsplit(url).path.rstrip("/")):
            raise SkipPage("不是该来源文章 URL")
        response = self.http.get(url)
        return parse_article(response.url or url,response.text,self.source)

def recover_raw(cfg: AppConfig, store) -> int:
    """Recover a complete immutable raw file written before a DB commit."""
    recovered = 0
    for path in cfg.resolve(cfg.output.raw_dir).glob("*/*/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            post = RawPost(**{k:data[k] for k in RawPost.__dataclass_fields__})
            if post.source not in cfg.sources or not iso_date(post.publish_time):
                continue
            if not store.has_post(post.url,post.publish_time):
                store.upsert_post(post,raw_path=path)
                recovered += 1
        except (ValueError,KeyError,TypeError,OSError):
            logger.warning("跳过无效 raw 文件: %s",path.name)
    return recovered

def collect_incremental(cfg: AppConfig, days: int | None = None, sources: list[str] | None = None) -> int:
    from .store import Store
    days = days if days is not None else cfg.collect.days
    if days <= 0:
        raise ValueError("days 必须为正数")
    since = now().date() - timedelta(days=days-1)
    total = 0
    with Store(cfg.resolve(cfg.output.db_path)) as store:
        recover_raw(cfg,store)
        for source in (sources or list(cfg.sources)):
            if not cfg.sources[source].enabled:
                continue
            if source == "github":
                from .github import GitHubCollector
                collector = GitHubCollector(cfg,days)
            else:
                collector = PublicCollector(cfg,source)
            try:
                if store.collected_today() >= cfg.collect.daily_limit:
                    break
                if store.collected_today(source) >= cfg.sources[source].max_posts:
                    continue
                for url in collector.discover():
                    if store.collected_today() >= cfg.collect.daily_limit or store.collected_today(source) >= cfg.sources[source].max_posts:
                        break
                    if store.attempted_today(url):
                        continue
                    try:
                        post = collector.fetch(url)
                        day = datetime.fromisoformat(post.publish_time.replace("Z","+00:00")).date()
                        if not since <= day <= now().date():
                            store.record_fetch(url,source,"skipped","outside_time_window")
                            continue
                        if store.has_post(post.url,post.publish_time):
                            store.record_fetch(url,source,"seen")
                            continue
                        path = save_raw(cfg.resolve(cfg.output.raw_dir),post)
                        store.upsert_post(post,raw_path=path)
                        store.record_fetch(url,source,"collected")
                        total += 1
                        logger.info("[%s] 已采集 %s",source,post.title)
                    except SourceUnavailable as exc:
                        store.record_fetch(url,source,"source_unavailable",str(exc))
                        raise
                    except (SkipPage,ValueError) as exc:
                        store.record_fetch(url,source,"skipped",str(exc))
                        logger.warning("[%s] 跳过 %s: %s",source,url,exc)
            except (SourceUnavailable,SkipPage) as exc:
                logger.warning("[%s] 本次暂停: %s",source,exc)
                store.record_fetch(f"source:{source}",source,"source_unavailable",str(exc))
            except Exception as exc:
                logger.error("[%s] 来源失败，不影响其他来源: %s",source,type(exc).__name__)
                store.record_fetch(f"source:{source}",source,"failed",type(exc).__name__)
            finally:
                collector.http.close()
    logger.info("采集完成: 新增 %d 帖",total)
    return total

def main() -> int:
    import sys
    from .__main__ import main as cli
    return cli(["collect",*sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(main())
