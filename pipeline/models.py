"""数据模型:流水线各层之间传递的结构化记录。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QuestionRecord:
    """一道被抽取出的面试题(已归一化、可跨帖去重)。"""

    hash: str
    post_url: str
    company: str
    position: str
    position_category: str  # java_backend | agent_ai | other
    round_name: str
    round_date: str | None
    question: str
    q_type: str  # 场景设计 | 八股 | 项目追问 | 手撕代码 | 开放问答
    follow_ups: str = "[]"  # JSON 数组序列化后的追问链
    original_text: str = ""
    first_seen: str = ""
    last_seen: str = ""
    times_seen: int = 1


@dataclass
class ExtractionResult:
    """LLM 对单帖的抽取结果(通过 JSON Schema 校验后的产物)。"""

    post_url: str
    company: str = ""
    department: str = ""
    position: str = ""
    position_category: str = "other"
    rounds: list[dict] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0
    is_interview_post: bool = True
