"""结构化存储:SQLite 单文件库。

表:
- posts     原始帖元数据与流水线状态(raw/filtered/extracted/extract_failed/discarded)
- questions 抽取后的题目,含 hash 用于跨帖去重与高频统计

每条记录的指纹为 URL(牛客公开帖 URL 不含无用 query,天然唯一)。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .collect import RawPost
from .models import QuestionRecord

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    url          TEXT PRIMARY KEY,
    post_id      TEXT NOT NULL,
    title        TEXT,
    author       TEXT,
    publish_time TEXT,
    crawl_time   TEXT,
    source       TEXT,
    status       TEXT NOT NULL DEFAULT 'raw',
    company      TEXT,
    position     TEXT,
    position_category TEXT,
    summary      TEXT,
    confidence   REAL,
    extract_time TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hash          TEXT NOT NULL,
    post_url      TEXT NOT NULL REFERENCES posts(url),
    company       TEXT,
    position      TEXT,
    position_category TEXT,
    round_name    TEXT,
    round_date    TEXT,
    question      TEXT NOT NULL,
    q_type        TEXT,
    follow_ups    TEXT,
    original_text TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    times_seen    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_questions_hash ON questions(hash);
CREATE INDEX IF NOT EXISTS idx_questions_category ON questions(position_category);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
"""


class Store:
    """SQLite 访问封装;每次实例化自动建表。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._raw_root: Path | None = None

    def close(self) -> None:
        self.conn.close()

    # ---------- posts ----------

    def has_post(self, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def upsert_post(self, post: RawPost, status: str = "raw") -> None:
        self.conn.execute(
            """INSERT INTO posts (url, post_id, title, author, publish_time,
                                   crawl_time, source, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                   author=excluded.author, publish_time=excluded.publish_time""",
            (
                post.url,
                post.post_id,
                post.title,
                post.author,
                post.publish_time,
                post.crawl_time,
                post.source,
                status,
            ),
        )
        self.conn.commit()

    def set_status(self, url: str, status: str) -> None:
        self.conn.execute("UPDATE posts SET status = ? WHERE url = ?", (status, url))
        self.conn.commit()

    def update_extraction(
        self,
        url: str,
        company: str,
        position: str,
        position_category: str,
        summary: str,
        confidence: float,
    ) -> None:
        from datetime import datetime

        self.conn.execute(
            """UPDATE posts SET status='extracted', company=?, position=?,
               position_category=?, summary=?, confidence=?, extract_time=?
               WHERE url=?""",
            (
                company,
                position,
                position_category,
                summary,
                confidence,
                datetime.now().isoformat(timespec="seconds"),
                url,
            ),
        )
        self.conn.commit()

    def posts_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM posts WHERE status IN ({marks}) ORDER BY publish_time DESC",
            statuses,
        ).fetchall()

    def get_post_content(self, url: str) -> str:
        """从 raw 目录读回原始正文(原始数据永不变更,抽取重跑从这里来)。"""
        import json

        row = self.conn.execute(
            "SELECT post_id, crawl_time, source FROM posts WHERE url = ?", (url,)
        ).fetchone()
        if row is None or self._raw_root is None:
            return ""
        # raw 按采集日期分目录,重跑时在所有日期目录中回找
        candidates = sorted(
            self._raw_root.glob(f"{row['source']}/*/{row['post_id']}.json")
        )
        for cand in candidates:
            try:
                return json.loads(cand.read_text(encoding="utf-8"))["content_raw"]
            except (OSError, KeyError, ValueError):
                continue
        logger.warning("raw 目录中找不到正文: %s", url)
        return ""

    def bind_raw_root(self, raw_dir: Path) -> None:
        self._raw_root = raw_dir

    # ---------- questions ----------

    def find_question(self, qhash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM questions WHERE hash = ? ORDER BY last_seen DESC LIMIT 1",
            (qhash,),
        ).fetchone()

    def all_recent_hashes(self, days: int = 60) -> list[tuple[str, str]]:
        """近 N 天的 (hash, question) 用于相似去重比对。"""
        rows = self.conn.execute(
            """SELECT hash, question FROM questions
               WHERE last_seen >= date('now', ?)""",
            (f"-{days} days",),
        ).fetchall()
        return [(r["hash"], r["question"]) for r in rows]

    def insert_question(self, q: QuestionRecord) -> None:
        self.conn.execute(
            """INSERT INTO questions (hash, post_url, company, position,
                   position_category, round_name, round_date, question, q_type,
                   follow_ups, original_text, first_seen, last_seen, times_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                q.hash,
                q.post_url,
                q.company,
                q.position,
                q.position_category,
                q.round_name,
                q.round_date,
                q.question,
                q.q_type,
                q.follow_ups,
                q.original_text,
                q.first_seen,
                q.last_seen,
                q.times_seen,
            ),
        )
        self.conn.commit()

    def bump_question(self, qhash: str, post_url: str, today: str) -> None:
        """同题再次出现:合并计数(仅当来自不同帖子)。"""
        self.conn.execute(
            """UPDATE questions SET times_seen = times_seen + 1, last_seen = ?
               WHERE hash = ? AND post_url != ?""",
            (today, qhash, post_url),
        )
        self.conn.commit()

    # ---------- 渲染查询 ----------

    def questions_for_render(
        self, category: str | None = None, since: str | None = None
    ) -> list[sqlite3.Row]:
        sql = """SELECT q.*, p.url AS source_url, p.title AS post_title
                 FROM questions q JOIN posts p ON q.post_url = p.url
                 WHERE p.status = 'extracted'"""
        params: list = []
        if category:
            sql += " AND q.position_category = ?"
            params.append(category)
        if since:
            sql += " AND q.last_seen >= ?"
            params.append(since)
        sql += " ORDER BY q.company, q.round_name, q.id"
        return self.conn.execute(sql, params).fetchall()

    def distinct_companies(self, since: str | None = None) -> list[str]:
        sql = "SELECT DISTINCT company FROM questions WHERE company != ''"
        params: list = []
        if since:
            sql += " AND last_seen >= ?"
            params.append(since)
        return [r["company"] for r in self.conn.execute(sql, params)]

    def count_questions(self, since: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM questions"
        params: list = []
        if since:
            sql += " WHERE last_seen >= ?"
            params.append(since)
        return self.conn.execute(sql, params).fetchone()["c"]
