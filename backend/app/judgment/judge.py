"""JevJudge: the `Judge` Protocol (core/interfaces.py) backed by DecisionClient + policy.py.

Every method makes one System-1 request (the injection scan splits very large inputs into
chunks) and returns contract types. Code computes everything that is not a judgment:
recipients, internal vs external, known contacts, taint, thresholds, recovery strategy.

Failure behavior (Jev and Laya both unavailable -> JudgeUnavailable):
    gate_action        -> ASK "judge unavailable, failing safe", model="fallback-conservative"
    verify             -> passed=False, every check p=0
    classify_failure   -> ASK_HUMAN
    scan_untrusted     -> 0.5 per text
    rank_memories      -> the most recent N, p=0.5
    infer_tool_effect  -> most conservative effect consistent with the annotations, confidence 0
    needs_clarification-> (0.0, None): proceed; every write is still gated and will ASK
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.models import (
    Autonomy,
    CriterionCheck,
    EffectClass,
    ErrorKind,
    GateDecision,
    Intent,
    MemoryItem,
    RecoveryDecision,
    RecoveryStrategy,
    ToolError,
    ToolSpec,
    Verdict,
    Verification,
    args_hash,
)
from . import policy
from .client import DecisionClient, JudgeUnavailable
from .questions import (
    MAX_MEMORY_ITEMS,
    NOTHING_MISSING,
    RECIPIENT_KEYS,
    chunk_texts,
    clarification_battery,
    effect_battery,
    failure_battery,
    gate_battery,
    memory_battery,
    scan_battery,
    verification_battery,
)

FALLBACK_MODEL = "fallback-conservative"
UNKNOWN_SIGNAL = 0.5  # used when an asked question comes back without an answer

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# Audience analysis (code, not model)
# ---------------------------------------------------------------------------


@dataclass
class Audience:
    emails: list[str] = field(default_factory=list)  # lower-cased addresses
    channels: list[str] = field(default_factory=list)  # chat channels (workspace audience)
    unparsed: list[str] = field(default_factory=list)  # names we could not resolve to an address
    external: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return [*self.emails, *self.channels, *self.unparsed]


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def _is_internal(email: str, internal_domain: str) -> bool:
    d, internal = _domain_of(email), internal_domain.lower().lstrip("@")
    return bool(internal) and (d == internal or d.endswith("." + internal))


def _is_known(email: str, known: set[str], known_domains: set[str]) -> bool:
    d = _domain_of(email)
    return email in known or any(d == k or d.endswith("." + k) for k in known_domains)


def analyze_audience(args: dict[str, Any], effect: EffectClass, known_contacts: list[str],
                     internal_domain: str) -> Audience:
    """Collect recipients from to/cc/bcc/attendees/channel/... and classify them.

    Only COMMUNICATE actions have an audience that receives anything; for other effects the
    recipients are listed (so Jev sees them) but external/unknown stay empty (a draft is not sent).
    known_contacts entries may be addresses or domains ("acme.dev" / "@acme.dev")."""
    aud = Audience()
    for key in RECIPIENT_KEYS:
        value = args.get(key)
        if value in (None, "", []):
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if isinstance(item, dict):
                item = item.get("email") or item.get("address") or item.get("id") or item.get("name") or ""
            text = str(item).strip()
            if not text:
                continue
            found = [e.lower() for e in _EMAIL_RE.findall(text)]
            if found:
                aud.emails.extend(e for e in found if e not in aud.emails)
            elif key == "channel" or text.startswith("#"):
                aud.channels.append(text)
            else:
                aud.unparsed.append(text)
    if effect != EffectClass.COMMUNICATE:
        return aud
    known = {c.strip().lower() for c in known_contacts if c and "@" in c.strip()[1:]}
    known_domains = {c.strip().lower().lstrip("@") for c in known_contacts if c and "@" not in c.strip()[1:]}
    for e in aud.emails:
        if not _is_internal(e, internal_domain):
            aud.external.append(e)
        if not _is_known(e, known, known_domains):
            aud.unknown.append(e)
    # A name we could not resolve to an address could be anyone: treat it as external and unknown.
    for name in aud.unparsed:
        aud.external.append(f"'{name}'")
        aud.unknown.append(f"'{name}'")
    return aud


def _describe_source(texts: list[str]) -> str:
    """Short user-facing label for where tainted content came from (used in gate reasons)."""
    sample = "\n".join(t[:600] for t in texts[:4])
    if re.search(r"(?im)^\s*(from|subject|to|sent):", sample) or re.search(r"(?i)\b(dear|regards|best,)\b", sample):
        return "an inbound email"
    if re.search(r"(?i)\b(transcript|speaker \d|action items?)\b", sample):
        return "a meeting transcript"
    return "external content the agent read"


def _effect_from_annotations(annotations: dict[str, Any]) -> EffectClass | None:
    """What the MCP hints claim, when they claim something coherent (hints are untrusted)."""
    if not annotations:
        return None
    ro, de = annotations.get("readOnlyHint"), annotations.get("destructiveHint")
    if ro is True:
        return None if de is True else EffectClass.READ
    if de is True:
        return EffectClass.WRITE_IRREVERSIBLE
    if de is False:
        return EffectClass.WRITE_REVERSIBLE
    return None


def conservative_effect(annotations: dict[str, Any]) -> EffectClass:
    """Most conservative effect consistent with the annotations (MCP defaults: readOnly=false,
    destructive=true, openWorld=true)."""
    if not annotations:
        return EffectClass.WRITE_IRREVERSIBLE
    ro, de = annotations.get("readOnlyHint"), annotations.get("destructiveHint")
    if ro is True and de is not True:
        return EffectClass.READ
    if de is False:
        return EffectClass.COMMUNICATE if annotations.get("openWorldHint", True) is not False else EffectClass.WRITE_REVERSIBLE
    return EffectClass.WRITE_IRREVERSIBLE


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


class JevJudge:
    """System-1 judge. `trust_tool_kind`: when a tool already classified its own error
    (ToolError.kind != unknown), use that label without a model call."""

    def __init__(self, client: DecisionClient | None = None, *, trust_tool_kind: bool = True) -> None:
        self.client = client or DecisionClient()
        self.trust_tool_kind = trust_tool_kind

    # -- clarification ----------------------------------------------------------------
    async def needs_clarification(self, request: str, intent: Intent, context: dict[str, Any]) -> tuple[float, str | None]:
        state, questions, options = clarification_battery(request, intent, context or {})
        try:
            ans = await self.client.ask("needs_clarification", state, questions)
        except JudgeUnavailable:
            return 0.0, None
        p = round(ans.noul("missing", 0.0), 4)
        which: str | None = None
        if "which" in ans.choices:
            choice = ans.choices["which"][0]
            if choice != NOTHING_MISSING:
                which = options.get(choice, choice)
        return p, which

    # -- action gate --------------------------------------------------------------------
    async def gate_action(
        self,
        *,
        user_request: str,
        intent: Intent,
        tool: ToolSpec,
        args: dict[str, Any],
        preview: dict[str, Any],
        untrusted_context: list[str],
        tainted: bool,
        autonomy: Autonomy,
        tainted_args: list[str] | None = None,
        known_contacts: list[str],
        internal_domain: str,
    ) -> GateDecision:
        level = Autonomy(autonomy)
        ahash = args_hash(tool.name, args)
        # Per-argument taint (SPEC section 6): only CONTROL args (recipients, channel, url, parent,
        # *_id targets) forbid AUTO. Unknown provenance (old callers) counts as control taint.
        tainted = bool(tainted or tainted_args)
        control_tainted = bool(policy.control_args(tainted_args)) if tainted_args is not None else tainted
        if tool.effect == EffectClass.READ:
            return GateDecision(verdict=Verdict.AUTO, risk=0.0, signals={}, reasons=["Read-only: no side effects"],
                                autonomy=level.value, model="policy", args_hash=ahash)

        aud = analyze_audience(args, tool.effect, known_contacts, internal_domain)
        comm = tool.effect == EffectClass.COMMUNICATE
        # Unknown audience for a communication (no recognizable recipient field) is treated as unknown.
        no_audience = comm and not aud.all
        external = bool(aud.external)
        unknown_recipient = bool(aud.unknown) or no_audience
        code_signals = {"external": float(external), "unknown_recipient": float(unknown_recipient),
                        "tainted": float(tainted), "control_tainted": float(control_tainted)}

        content = {k: v for k, v in args.items() if k not in RECIPIENT_KEYS}
        extra = {k: v for k, v in (preview or {}).items()
                 if k not in args and k not in RECIPIENT_KEYS and k not in ("kind", "id", "status")}
        if extra:
            content["preview"] = extra
        texts = [t for t in (untrusted_context or []) if t and t.strip()]
        state, questions = gate_battery(user_request=user_request, intent=intent, tool=tool, content=content,
                                        recipients=aud.all, untrusted_context=texts)
        try:
            ans = await self.client.ask("gate", state, questions)
        except JudgeUnavailable:
            verdict, risk, reasons = policy.fallback_gate(tool.effect)
            return GateDecision(verdict=verdict, risk=risk, signals=code_signals, reasons=reasons,
                                autonomy=level.value, model=FALLBACK_MODEL, args_hash=ahash)

        signals: dict[str, float] = {"alignment": round(ans.score_norm("alignment", UNKNOWN_SIGNAL), 4)}
        if "alignment" in ans.scores:
            signals["alignment_confidence"] = round(ans.scores["alignment"][1], 4)
        if "injection" in questions:
            signals["injection"] = round(ans.noul("injection", UNKNOWN_SIGNAL), 4)
        else:
            # Nothing untrusted to compare against. If the args are tainted anyway, we cannot rule it out.
            signals["injection"] = UNKNOWN_SIGNAL if tainted else 0.0
        signals["sensitive"] = round(ans.noul("sensitive", UNKNOWN_SIGNAL), 4)
        if comm:
            signals["tone_ok"] = round(ans.noul("tone_ok", UNKNOWN_SIGNAL), 4)
            if "recipients_match" in questions:
                signals["recipients_match"] = round(ans.noul("recipients_match", UNKNOWN_SIGNAL), 4)
        signals.update(code_signals)

        verdict, risk, reasons = policy.decide_gate(
            tool.effect, signals, level, tainted, external, unknown_recipient,
            external_recipients=aud.external, unknown_recipients=aud.unknown, internal_domain=internal_domain,
            untrusted_source=_describe_source(texts) if texts else "untrusted content the agent read",
            degraded=ans.degraded, tainted_args=tainted_args, control_tainted=control_tainted,
        )
        if no_audience:
            reasons.append("Could not determine who receives this message")
        if tainted and "injection" not in questions:
            reasons.append("Built from untrusted content that was not available for inspection")
        return GateDecision(verdict=verdict, risk=risk, signals=signals, reasons=reasons, autonomy=level.value,
                            model=ans.model, args_hash=ahash)

    # -- proof of done ------------------------------------------------------------------
    async def verify(self, *, goal: str, criteria: list[str], evidence: dict[str, Any]) -> Verification:
        crit = [c for c in criteria if c and c.strip()]
        if not crit:
            return Verification(passed=True, checks=[], model="none")
        state, questions = verification_battery(goal, crit, evidence)
        try:
            ans = await self.client.ask("verify", state, questions)
        except JudgeUnavailable:
            return Verification(passed=False, model=FALLBACK_MODEL,
                                checks=[CriterionCheck(criterion=c, p=0.0, passed=False) for c in crit])
        threshold = policy.THRESHOLDS["verify_pass"]
        checks = []
        for i, c in enumerate(crit):
            p = round(ans.noul(f"c{i}", 0.0), 4)
            checks.append(CriterionCheck(criterion=c, p=p, passed=p >= threshold))
        return Verification(passed=all(ch.passed for ch in checks), checks=checks, model=ans.model)

    # -- failure recovery ----------------------------------------------------------------
    async def classify_failure(self, *, tool: ToolSpec, args: dict[str, Any], error: ToolError, attempts: int,
                               optional: bool, alternatives: list[str]) -> RecoveryDecision:
        if self.trust_tool_kind and error.kind != ErrorKind.UNKNOWN:
            strategy, note = policy.recovery_strategy(error.kind, 1.0, attempts, optional, alternatives)
            return RecoveryDecision(cause=error.kind, strategy=strategy, confidence=1.0,
                                    probabilities={error.kind.value: 1.0}, note=f"{note} (classified by the tool)")
        state, questions = failure_battery(tool, args, error.message, attempts, error.retryable)
        try:
            ans = await self.client.ask("classify_failure", state, questions)
        except JudgeUnavailable:
            return RecoveryDecision(cause=error.kind, strategy=RecoveryStrategy.ASK_HUMAN, confidence=0.0,
                                    note="judge unavailable, failing safe")
        if "cause" not in ans.choices:
            return RecoveryDecision(cause=ErrorKind.UNKNOWN, strategy=RecoveryStrategy.ASK_HUMAN, confidence=0.0,
                                    note="no classification returned")
        choice, confidence, probs = ans.choices["cause"]
        cause = ErrorKind(choice)
        strategy, note = policy.recovery_strategy(cause, confidence, attempts, optional, alternatives)
        return RecoveryDecision(cause=cause, strategy=strategy, confidence=round(confidence, 4),
                                probabilities={k: round(v, 4) for k, v in probs.items()}, note=note)

    # -- injection scan -----------------------------------------------------------------
    async def scan_untrusted(self, texts: dict[str, str]) -> dict[str, float]:
        texts = {k: v for k, v in (texts or {}).items() if isinstance(v, str)}
        if not texts:
            return {}

        async def run(chunk: dict[str, str]) -> dict[str, float]:
            state, questions, groups = scan_battery(chunk)
            try:
                ans = await self.client.ask("scan_untrusted", state, questions)
            except JudgeUnavailable:
                return {tid: UNKNOWN_SIGNAL for tid in chunk}
            return {tid: round(max(ans.noul(q, UNKNOWN_SIGNAL) for q in qids), 4) for tid, qids in groups.items()}

        out: dict[str, float] = {}
        for part in await asyncio.gather(*(run(c) for c in chunk_texts(texts))):
            out.update(part)
        return out

    # -- memory ---------------------------------------------------------------------------
    async def rank_memories(self, request: str, items: list[MemoryItem], limit: int = 6) -> list[tuple[MemoryItem, float]]:
        if not items or limit <= 0:
            return []
        recent = sorted(items, key=lambda m: m.created_at, reverse=True)[:MAX_MEMORY_ITEMS]
        state, questions = memory_battery(request, recent)
        try:
            ans = await self.client.ask("rank_memories", state, questions)
        except JudgeUnavailable:
            return [(m, UNKNOWN_SIGNAL) for m in recent[:limit]]
        floor = policy.THRESHOLDS["memory_min"]
        scored = [(m, round(ans.noul(f"m{i}", 0.0), 4)) for i, m in enumerate(recent)]
        scored = [(m, p) for m, p in scored if p >= floor]
        scored.sort(key=lambda mp: mp[1], reverse=True)
        return scored[:limit]

    # -- MCP effect inference -------------------------------------------------------------
    async def infer_tool_effect(self, name: str, description: str, annotations: dict[str, Any]) -> tuple[str, float]:
        annotations = annotations or {}
        state, questions = effect_battery(name, description, annotations)
        try:
            ans = await self.client.ask("infer_tool_effect", state, questions)
        except JudgeUnavailable:
            return conservative_effect(annotations).value, 0.0
        if "effect" not in ans.choices:
            return conservative_effect(annotations).value, 0.0
        choice, confidence, probs = ans.choices["effect"]
        # Keep every effect with real probability mass, plus whatever the hints claim, and take the
        # most conservative: mislabeling a destructive tool as read-only is the costly error.
        plausible = {EffectClass(choice)} | {EffectClass(k) for k, p in probs.items() if p >= 0.2}
        hinted = _effect_from_annotations(annotations)
        if hinted is not None:
            plausible.add(hinted)
        effect = policy.most_conservative(*plausible)
        conf = confidence if effect.value == choice else probs.get(effect.value, 0.0)
        return effect.value, round(float(conf), 4)
