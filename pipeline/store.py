"""SQLite versioned posts, canonical questions and per-source occurrences."""
from __future__ import annotations
import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path
from .config import DedupConfig
from .dedup import is_duplicate, normalize_question, question_hash
from .models import ExtractionResult
from .runtime import now, iso_date

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
 fingerprint TEXT PRIMARY KEY, url TEXT NOT NULL, post_id TEXT NOT NULL,
 title TEXT, author TEXT, publish_time TEXT NOT NULL, crawl_time TEXT NOT NULL,
 source TEXT NOT NULL, raw_path TEXT, status TEXT NOT NULL DEFAULT 'raw',
 company TEXT, position TEXT, position_category TEXT, summary TEXT,
 confidence REAL, extract_time TEXT, error TEXT, extraction_json TEXT,
 UNIQUE(url, publish_time)
);
CREATE TABLE IF NOT EXISTS questions (
 hash TEXT PRIMARY KEY, question TEXT NOT NULL,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, times_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS occurrences (
 id INTEGER PRIMARY KEY, question_hash TEXT NOT NULL REFERENCES questions(hash),
 post_fingerprint TEXT NOT NULL REFERENCES posts(fingerprint),
 company TEXT, position TEXT, position_category TEXT, round_name TEXT NOT NULL,
 round_date TEXT NOT NULL DEFAULT '', question TEXT NOT NULL, q_type TEXT,
 follow_ups TEXT NOT NULL DEFAULT '[]', original_text TEXT, event_date TEXT NOT NULL,
 UNIQUE(question_hash, post_fingerprint, round_name, round_date)
);
CREATE TABLE IF NOT EXISTS fetch_attempts (
 url TEXT PRIMARY KEY, source TEXT NOT NULL, attempted_at TEXT NOT NULL,
 outcome TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 command TEXT NOT NULL, status TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS v2_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS v2_posts_url ON posts(url);
CREATE INDEX IF NOT EXISTS v2_occurrences_date ON occurrences(event_date);
CREATE INDEX IF NOT EXISTS v2_occurrences_post ON occurrences(post_fingerprint);
"""

def fingerprint(url: str, publish_time: str) -> str:
    return hashlib.sha256(f"{url}\n{publish_time}".encode()).hexdigest()

class Store:
    """A whole post commits atomically; migration first backs up SQLite."""
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._raw_root: Path | None = None
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if columns and "fingerprint" not in columns:
            self._migrate_legacy()
        else:
            self.conn.executescript(SCHEMA)
            self.conn.execute("PRAGMA user_version=2")
            self.conn.commit()

    def _migrate_legacy(self) -> None:
        backup = self.path.with_name(f"{self.path.stem}.pre-v2-{now().strftime('%Y%m%d-%H%M%S')}.db")
        dest = sqlite3.connect(str(backup))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        posts = [dict(r) for r in self.conn.execute("SELECT * FROM posts")]
        try:
            self.conn.executescript("BEGIN IMMEDIATE; ALTER TABLE questions RENAME TO legacy_questions; ALTER TABLE posts RENAME TO legacy_posts;" + SCHEMA)
            for p in posts:
                fp = fingerprint(p["url"], p["publish_time"] or "")
                status = "to_extract" if p["status"] in ("extracted","pending_review") else p["status"]
                self.conn.execute("""INSERT INTO posts(fingerprint,url,post_id,title,author,publish_time,crawl_time,source,status,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (fp,p["url"],p["post_id"],p["title"],p["author"],p["publish_time"] or "",p["crawl_time"],p["source"],status,"legacy_requires_reextraction"))
            self.conn.execute("PRAGMA user_version=2")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        logger.warning("旧库备份: %s；旧题保存在 legacy_questions，%d 帖迁移后待重新抽取/复核", backup, len(posts))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def has_post(self, url: str, publish_time: str | None = None) -> bool:
        if publish_time is None:
            return self.conn.execute("SELECT 1 FROM posts WHERE url=?", (url,)).fetchone() is not None
        return self.conn.execute("SELECT 1 FROM posts WHERE fingerprint=?", (fingerprint(url,publish_time),)).fetchone() is not None

    def upsert_post(self, post, status: str = "raw", raw_path: Path | None = None) -> str:
        fp = fingerprint(post.url, post.publish_time or "")
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO posts(fingerprint,url,post_id,title,author,publish_time,crawl_time,source,status,raw_path)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (fp,post.url,post.post_id,post.title,post.author,post.publish_time or "",post.crawl_time,post.source,status,str(raw_path) if raw_path else None))
        return fp

    def set_status(self, fp: str, status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE posts SET status=?,error=? WHERE fingerprint=?", (status,error,fp))

    def posts_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        if not statuses:
            return []
        return self.conn.execute(f"SELECT * FROM posts WHERE status IN ({','.join('?' for _ in statuses)}) ORDER BY publish_time DESC",statuses).fetchall()

    def get_post(self, fp: str):
        return self.conn.execute("SELECT * FROM posts WHERE fingerprint=?", (fp,)).fetchone()

    def bind_raw_root(self, raw_dir: Path) -> None:
        self._raw_root = raw_dir

    def get_post_content(self, fp: str) -> str:
        row = self.get_post(fp)
        if row is None or self._raw_root is None:
            return ""
        candidates = [Path(row["raw_path"])] if row["raw_path"] else []
        candidates.extend(sorted(self._raw_root.glob(f"{row['source']}/*/{row['post_id']}*.json")))
        for candidate in candidates:
            if not candidate.resolve().is_relative_to(self._raw_root.resolve()):
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if data.get("url") == row["url"] and (data.get("publish_time") or "") == row["publish_time"]:
                    return data["content_raw"]
            except (OSError, ValueError, KeyError):
                continue
        return ""

    def collected_today(self, source: str | None = None) -> int:
        sql = "SELECT count(*) FROM posts WHERE substr(crawl_time,1,10)=?"
        params = [now().date().isoformat()]
        if source:
            sql += " AND source=?"
            params.append(source)
        return self.conn.execute(sql, params).fetchone()[0]

    def attempted_today(self, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM fetch_attempts WHERE url=? AND substr(attempted_at,1,10)=?", (url,now().date().isoformat())).fetchone() is not None

    def record_fetch(self, url: str, source: str, outcome: str, reason: str = "") -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO fetch_attempts VALUES(?,?,?,?,?)",(url,source,now().isoformat(),outcome,reason))

    def all_recent_hashes(self, days: int | None = None):
        """All-time dedup; retention windows must not create duplicates."""
        return [(r[0],r[1]) for r in self.conn.execute("SELECT hash,question FROM questions")]

    def save_extraction(self, fp: str, result: ExtractionResult, dedup: DedupConfig) -> int:
        row = self.get_post(fp)
        if row is None:
            raise ValueError("未知帖子")
        pub_date = iso_date(row["publish_time"])
        if not pub_date:
            raise ValueError("缺少可靠发布日期，不能进入时效统计")
        pairs = self.all_recent_hashes()
        count = 0
        with self.conn:
            self.conn.execute("DELETE FROM occurrences WHERE post_fingerprint=?",(fp,))
            for rnd in result.rounds:
                for q in rnd["questions"]:
                    norm = normalize_question(q["question"],dedup.synonyms)
                    h = is_duplicate(norm,pairs,dedup.threshold,dedup.short_len,dedup.synonyms)
                    if h is None:
                        h = question_hash(q["question"],dedup.synonyms)
                        self.conn.execute("INSERT OR IGNORE INTO questions VALUES(?,?,?,?,0)",(h,q["question"],pub_date,pub_date))
                        pairs.append((h,q["question"]))
                    rd = rnd.get("date") or ""
                    event = rd or pub_date
                    old = self.conn.execute("SELECT id,follow_ups,original_text FROM occurrences WHERE question_hash=? AND post_fingerprint=? AND round_name=? AND round_date=?",(h,fp,rnd["round_name"],rd)).fetchone()
                    if old:
                        follow = list(dict.fromkeys(json.loads(old["follow_ups"]) + q.get("follow_ups",[])))
                        evidence = old["original_text"] or q.get("original_text", "")
                        self.conn.execute("UPDATE occurrences SET follow_ups=?,original_text=? WHERE id=?",(json.dumps(follow,ensure_ascii=False),evidence,old["id"]))
                    else:
                        self.conn.execute("""INSERT INTO occurrences(question_hash,post_fingerprint,company,position,position_category,round_name,round_date,question,q_type,follow_ups,original_text,event_date)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(h,fp,result.company,result.position,result.position_category,rnd["round_name"],rd,q["question"],q["type"],json.dumps(q.get("follow_ups",[]),ensure_ascii=False),q.get("original_text",""),event))
                        count += 1
            self.conn.execute("UPDATE posts SET status='extracted',company=?,position=?,position_category=?,summary=?,confidence=?,extract_time=?,error=NULL,extraction_json=? WHERE fingerprint=?",(result.company,result.position,result.position_category,result.summary,result.confidence,now().isoformat(),json.dumps(asdict(result),ensure_ascii=False),fp))
            self.conn.execute("DELETE FROM questions WHERE hash NOT IN (SELECT question_hash FROM occurrences)")
            self.conn.execute("""UPDATE questions SET times_seen=(SELECT count(DISTINCT p.url) FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint WHERE o.question_hash=questions.hash),
                first_seen=(SELECT min(event_date) FROM occurrences WHERE question_hash=questions.hash),
                last_seen=(SELECT max(event_date) FROM occurrences WHERE question_hash=questions.hash)""")
        return count

    def questions_for_render(self, category: str | None = None, since: str | None = None):
        sql = """SELECT o.*,o.question_hash AS hash,p.url AS source_url,p.title AS post_title,
            p.publish_time,p.fingerprint FROM occurrences o JOIN posts p ON p.fingerprint=o.post_fingerprint
            WHERE p.status='extracted' AND p.publish_time=(SELECT max(p2.publish_time) FROM posts p2 WHERE p2.url=p.url AND p2.status='extracted')"""
        params = []
        if category:
            sql += " AND o.position_category=?"
            params.append(category)
        if since:
            sql += " AND o.event_date>=?"
            params.append(since)
        return self.conn.execute(sql+" ORDER BY o.company,o.round_name,o.id",params).fetchall()

    def status_summary(self) -> dict:
        return {
            "posts": {r[0]:r[1] for r in self.conn.execute("SELECT status,count(*) FROM posts GROUP BY status")},
            "sources": {r[0]:r[1] for r in self.conn.execute("SELECT source,count(*) FROM posts GROUP BY source")},
            "questions": self.conn.execute("SELECT count(*) FROM questions").fetchone()[0],
            "occurrences": self.conn.execute("SELECT count(*) FROM occurrences").fetchone()[0],
        }
