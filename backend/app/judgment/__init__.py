"""System-1 judgment layer: Jev (TypeSafe) with Laya as the open-weight fallback.

    from app.judgment import get_judge, describe
    judge = get_judge()                    # JevJudge, implements core.interfaces.Judge
    decision = await judge.gate_action(...)
    describe()                             # policy weights/thresholds for GET /api/config
"""
from __future__ import annotations

from .client import DecisionClient, JudgeUnavailable, JudgmentAnswers
from .judge import JevJudge
from .policy import THRESHOLDS, WEIGHTS, decide_gate, describe, recovery_strategy

_judge: JevJudge | None = None


def get_judge() -> JevJudge:
    """Process-wide judge (shares one Jev client, one answer cache and one Laya instance)."""
    global _judge
    if _judge is None:
        _judge = JevJudge()
    return _judge


__all__ = [
    "THRESHOLDS",
    "WEIGHTS",
    "DecisionClient",
    "JevJudge",
    "JudgeUnavailable",
    "JudgmentAnswers",
    "decide_gate",
    "describe",
    "get_judge",
    "recovery_strategy",
]
