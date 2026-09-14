"""Persistent per-call token reservations; ambiguous failures stay charged."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from .runtime import atomic_json, now

class BudgetPaused(RuntimeError):
    pass

class TokenBudget:
    def __init__(self, usage_path: Path, daily_budget: int):
        self.path = usage_path
        self.daily_budget = daily_budget
        self.data = {}
        self._load()

    def _load(self):
        today = now().date().isoformat()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data,dict) or not isinstance(data.get("used",0),int) or data.get("used",0)<0:
                    raise ValueError("invalid token ledger")
            except (ValueError,OSError) as exc:
                raise BudgetPaused("Token 账本损坏；暂停，不能将未知用量当作零") from exc
            if data.get("date") == today:
                self.data = data
                self.data.setdefault("calls",[])
                return
            # Retain history for per-post cost auditing across days.
            archive = self.path.with_name(f"usage-{data.get('date','unknown')}.json")
            if not archive.exists():
                atomic_json(archive,data)
        self.data = {"date":today,"used":0,"calls":[]}

    @property
    def used_today(self) -> int:
        self._load()
        return self.data["used"]

    def reserve(self, amount: int, post_id: str, chunk: int, attempt: int) -> str:
        self._load()
        if self.data["used"]+amount>self.daily_budget:
            raise BudgetPaused(f"今日已用/保留 {self.data['used']} tokens，下一次最多需 {amount}，超过日预算 {self.daily_budget}")
        call_id = uuid.uuid4().hex
        self.data["used"] += amount
        self.data["calls"].append({"id":call_id,"post":post_id,"chunk":chunk,"attempt":attempt,"reserved":amount,"charged":amount,"status":"reserved","at":now().isoformat()})
        atomic_json(self.path,self.data)
        return call_id

    def settle(self, call_id: str, tokens: int | None):
        # Uses the reservation day even when the request crossed midnight.
        call = next(c for c in self.data["calls"] if c["id"]==call_id)
        if tokens is None:
            call["status"] = "usage_unknown_reserved"
        else:
            self.data["used"] += tokens-call["charged"]
            call.update(charged=tokens,status="completed")
        atomic_json(self.path,self.data)
