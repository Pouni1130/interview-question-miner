"""精去重:题目标准化 + hash 精确匹配 + shingling/Jaccard 相似去重。"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# 同义归并词表:标准化前替换(维护在代码内,规模小、变更少)
SYNONYMS = {
    "中间件": "",
    "请问": "",
    "问一下": "",
    "面试官问": "",
    "面试官让我": "",
    "讲一下": "",
    "说说": "",
    "介绍一下": "",
    "如何理解": "如何",
    "你怎么理解": "如何",
    "你的理解": "如何",
}

_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def normalize_question(text: str) -> str:
    """题目标准化:同义归并 → 全角转半角 → 去标点 → 转小写 → 去空白。"""
    t = text.strip()
    for src, dst in SYNONYMS.items():
        t = t.replace(src, dst)
    t = unicodedata.normalize("NFKC", t)
    t = _PUNCT_RE.sub("", t)
    return t.lower().strip()


def question_hash(text: str) -> str:
    """标准化后的 md5,用于跨帖精确去重与高频统计。"""
    return hashlib.md5(normalize_question(text).encode("utf-8")).hexdigest()


def _char_shingles(text: str, k: int = 3) -> set[str]:
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """字符 3-gram Jaccard 相似度;短串(标准化后 <6 字)返回 0 由精确匹配兜底。"""
    sa, sb = _char_shingles(a), _char_shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_duplicate(
    normalized_new: str,
    existing: list[tuple[str, str]],
    threshold: float = 0.9,
    short_len: int = 30,
) -> str | None:
    """判断新题(已标准化)是否与既有题目重复。

    规则(需求文档 4.2):相似度 > threshold 且长度 < short_len 视为重复。
    existing 为 [(hash, question_raw)] 列表。返回命中的 hash 或 None。
    """
    for qhash, raw in existing:
        norm = normalize_question(raw)
        if not norm or not normalized_new:
            continue
        # 完全一致(不限长度)或 短题高相似
        if norm == normalized_new:
            return qhash
        if len(normalized_new) < short_len and jaccard_similarity(normalized_new, norm) > threshold:
            return qhash
    return None
