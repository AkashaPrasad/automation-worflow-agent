"""System-1 decision client: one typed request, many parallel questions.

    DecisionClient.ask(purpose, state, questions) -> JudgmentAnswers

Order of service:
  1. Jev (TypeSafe `AsyncTypeSafeClient.system_one`), ~20 s budget, the SDK retries 429/5xx.
  2. Laya (open-weight, local, optional): only when LAYA_ENABLED is not "false" and `laya` is
     importable. Runs in a worker thread (it is synchronous), with the state truncated to fit its
     512-token context and probabilities softened with a temperature (it ships over-confident).
  3. Otherwise raise `JudgeUnavailable`; the judge turns that into a conservative default.

Questions are plain dicts in the TypeSafe wire format ({"type": "noul"|"choice"|"score",
"instructions": ..., "criteria": ...}) so they hash stably and serve both backends. A question may
also carry a "laya" key with a short wording for Laya (see `laya_requests`); it is stripped before
the request goes to Jev. Every call (cache hit, miss or failure) emits a `judgment.call` trace.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..core.tracing import emit_trace

JEV_HTTP_TIMEOUT_S = 10.0  # per HTTP operation
JEV_TOTAL_BUDGET_S = 20.0  # SDK retry budget per call (initial attempt + retries + backoff)
JEV_HARD_TIMEOUT_S = 25.0  # asyncio guard around the whole SDK call
JEV_MAX_CONCURRENCY = 16  # 1,200 req/min account limit; keep bursts polite
JEV_STATE_CHAR_BUDGET = 60_000  # ~15k tokens; Jev allows 32k for state + longest question
CACHE_SIZE = 256

LAYA_TEMPERATURE = float(os.getenv("LAYA_TEMPERATURE", "1.5"))  # >1 softens over-confident outputs (not fitted)
LAYA_STATE_CHAR_BUDGET = 1_400  # ~350 tokens of the 512-token context; the rest is the question head
LAYA_FIELD_CHAR_BUDGET = 500


class JudgeUnavailable(RuntimeError):
    """Neither Jev nor Laya could answer."""


@dataclass
class JudgmentAnswers:
    """Normalized answers from either backend.

    nouls:   {id: p_yes}
    choices: {id: (choice, confidence, {option: p})}
    scores:  {id: (score, confidence, {level: p}, {level: description})}
    """

    nouls: dict[str, float] = field(default_factory=dict)
    choices: dict[str, tuple[str, float, dict[str, float]]] = field(default_factory=dict)
    scores: dict[str, tuple[float, float, dict[int, float], dict[int, Any]]] = field(default_factory=dict)
    model: str = ""  # "jev-1.13.0" | "laya"
    backend: str = "jev"  # "jev" | "laya"
    checkpoint: str = ""  # Laya checkpoint the router picked ("english", "multilingual")
    tokens: int = 0  # input tokens (output tokens are free on Jev)
    latency_ms: int = 0
    cost_usd: float = 0.0
    cached: bool = False

    @property
    def degraded(self) -> bool:
        """True when answered by the offline fallback rather than Jev."""
        return self.backend != "jev"

    def noul(self, qid: str, default: float) -> float:
        return self.nouls.get(qid, default)

    def score_norm(self, qid: str, default: float) -> float:
        """Score position divided by the top level, so every scale reads 0..1."""
        if qid not in self.scores:
            return default
        score, _conf, probs, _legend = self.scores[qid]
        top = max(probs) if probs else 1
        return max(0.0, min(1.0, score / top)) if top else default

    def compact(self) -> dict[str, Any]:
        """Small, JSON-safe summary for traces and the timeline."""
        out: dict[str, Any] = {k: round(v, 3) for k, v in self.nouls.items()}
        for k, (choice, conf, _p) in self.choices.items():
            out[k] = {"choice": choice, "confidence": round(conf, 3)}
        for k, (score, conf, _p, _l) in self.scores.items():
            out[k] = {"score": round(score, 3), "confidence": round(conf, 3)}
        return out


def _stable_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def clip_text(text: str, limit: int) -> str:
    """Keep the head and a little of the tail of a long string."""
    if len(text) <= limit:
        return text
    head = max(0, limit - limit // 5 - 5)
    return text[:head] + " [...] " + text[-(limit // 5):]


def fit_state(obj: Any, *, field_chars: int, max_items: int = 25) -> Any:
    """Recursively clip strings to `field_chars` and lists to `max_items`."""
    if isinstance(obj, str):
        return clip_text(obj, field_chars)
    if isinstance(obj, dict):
        return {k: fit_state(v, field_chars=field_chars, max_items=max_items) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        items = [fit_state(v, field_chars=field_chars, max_items=max_items) for v in list(obj)[:max_items]]
        if len(obj) > max_items:
            items.append(f"[{len(obj) - max_items} more items omitted]")
        return items
    return obj


def budget_state(state: Any, total_chars: int, field_chars: int) -> Any:
    """Shrink per-field limits until the serialized state fits `total_chars` (best effort).

    A last-resort guard; batteries already send only the fields their questions need."""
    fitted = fit_state(state, field_chars=field_chars)
    while len(json.dumps(fitted, default=str)) > total_chars and field_chars > 80:
        field_chars = int(field_chars * 0.6)
        fitted = fit_state(state, field_chars=field_chars, max_items=10)
    return fitted


# ---------------------------------------------------------------------------
# Laya adapter (optional, lazy)
# ---------------------------------------------------------------------------


class _Laya:
    """Lazy, thread-safe wrapper around `laya.Router`. Imported only when first needed."""

    def __init__(self) -> None:
        self._router: Any = None
        self._lock = threading.Lock()
        self.load_error: str | None = None
        self.load_seconds: float | None = None

    @staticmethod
    def importable() -> bool:
        return importlib.util.find_spec("laya") is not None

    def _get(self) -> Any:
        with self._lock:
            if self._router is None:
                if self.load_error:
                    raise RuntimeError(self.load_error)
                t0 = time.perf_counter()
                try:
                    from laya import Router  # heavy: pulls torch + transformers

                    router = Router(preload=os.getenv("LAYA_PRELOAD", "") == "1")
                    # Force the English checkpoint to load now so the first real call is fast.
                    router.predict("warm up", {"w": {"type": "noul", "instructions": "Is this a test?",
                                                     "criteria": {"true": "yes", "false": "no"}}})
                except Exception as e:  # missing weights, no network on first download, OOM...
                    self.load_error = f"laya failed to load: {type(e).__name__}: {e}"[:300]
                    raise RuntimeError(self.load_error) from e
                self._router = router
                self.load_seconds = round(time.perf_counter() - t0, 2)
            return self._router

    def warm(self) -> None:
        self._get()

    def predict(self, state: Any, questions: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str]:
        """Run every question as its own small request (see `laya_requests`) in one batch."""
        router = self._get()
        requests = laya_requests(state, questions)
        with self._lock:  # one forward pass at a time; the torch modules are shared
            results = router.predict_batch(requests) if len(requests) > 1 else [router.predict(**requests[0])]
        answers: dict[str, Any] = {}
        tokens = 0
        checkpoints: list[str] = []
        for res in results:
            answers.update(res.get("answers", {}))
            tokens += int((res.get("usage") or {}).get("input_tokens") or 0)
            routed = (res.get("routing") or {}).get("model")
            if routed and routed not in checkpoints:
                checkpoints.append(str(routed))
        return {"answers": answers, "usage": {"input_tokens": tokens}}, "+".join(checkpoints)


_PATH_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)[^`]*`")
_NOUL_DEFAULT = {"true": "Yes, this is the case.", "false": "No, this is not the case."}


def _short(value: Any, limit: int = 160) -> str | None:
    """Collapse a structured criterion ({"what": ..., "examples": [...]}) to its core text."""
    if value is None:
        return None
    if isinstance(value, dict):
        core = value.get("what") or value.get("summary") or json.dumps(value, default=str)
        examples = value.get("examples")
        if isinstance(examples, list) and examples:
            core = f"{core} (e.g. {', '.join(str(e) for e in examples[:3])})"
        value = core
    elif isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    return clip_text(" ".join(str(value).split()), limit)


def _laya_criteria(q: dict[str, Any]) -> Any:
    crit = q.get("criteria")
    if q.get("type") == "noul":
        # Laya's English checkpoint answers 'no' to every criteria-less noul (README, #156).
        crit = crit or _NOUL_DEFAULT
        return {"true": _short(crit.get("true")) or _NOUL_DEFAULT["true"],
                "false": _short(crit.get("false")) or _NOUL_DEFAULT["false"]}
    if q.get("type") == "choice":
        return {k: _short(v) for k, v in (crit or {}).items()}
    return [_short(v) or "" for v in (crit or [])]


def _resolve(state: dict[str, Any], path: str) -> Any:
    """'proposed_action.content' -> state['proposed_action']['content'] (None when absent)."""
    cur: Any = state
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def laya_requests(state: Any, questions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt a Jev battery to Laya's 512-token window (~192 for the question head).

    Measured on this project: Laya's base checkpoint discriminates well on short questions over
    a minimal state, and collapses toward 0.5 on long structured criteria over a mixed state.
    So each question becomes its own request whose state holds only the fields its backticked
    paths reference (the TypeSafe convention the batteries already follow), plus any data the
    question embeds in structured instructions; structured criteria shrink to their `what`.
    A question may carry a `laya` override with a short wording that was validated on Laya
    (it is stripped before the request goes to Jev). An override of type "choice" with
    `as_noul: <label>` is read back as a noul equal to that label's probability."""
    shared = state if isinstance(state, dict) else {"context": state}
    requests: list[dict[str, Any]] = []
    for qid, q in questions.items():
        override = q.get("laya")
        if isinstance(override, dict):
            # Battery-supplied short form: {"type"?, "instructions", "criteria", "fields": {name: "dotted.path"}};
            # paths resolve against the state plus any data embedded in the question's instructions.
            ins_data = q["instructions"] if isinstance(q.get("instructions"), dict) else {}
            pool = {**shared, **{k: v for k, v in ins_data.items() if k != "question"}}
            sub = {name: _resolve(pool, path) for name, path in (override.get("fields") or {}).items()}
            requests.append({
                "state": budget_state(sub or dict(shared), LAYA_STATE_CHAR_BUDGET, LAYA_FIELD_CHAR_BUDGET),
                "questions": {qid: {"type": override.get("type", q.get("type")),
                                    "instructions": override["instructions"], "criteria": override["criteria"]}},
            })
            continue
        ins = q.get("instructions")
        data: dict[str, Any] = {}
        if isinstance(ins, dict):
            text = str(ins.get("question") or json.dumps(ins, default=str))
            data = {k: v for k, v in ins.items() if k != "question"}
        else:
            text = str(ins or "")
        criteria = _laya_criteria(q)
        refs = set(_PATH_RE.findall(text + " " + json.dumps(criteria, default=str)))
        pool = {**shared, **data}
        sub = {k: v for k, v in pool.items() if k in refs} or dict(shared)
        requests.append({
            "state": budget_state(sub, LAYA_STATE_CHAR_BUDGET, LAYA_FIELD_CHAR_BUDGET),
            "questions": {qid: {"type": q.get("type"), "instructions": clip_text(text, 400), "criteria": criteria}},
        })
    return requests


def _temper(probs: list[float], temperature: float) -> list[float]:
    """p_i^(1/T) renormalized: temperature scaling applied to probabilities."""
    if temperature == 1.0 or not probs:
        return probs
    logs = [math.log(max(p, 1e-9)) / temperature for p in probs]
    m = max(logs)
    ex = [math.exp(v - m) for v in logs]
    s = sum(ex)
    return [v / s for v in ex]


def _peak_confidence(probs: list[float]) -> float:
    """Distribution concentration in [0, 1]: (k * max - 1) / (k - 1)."""
    k = len(probs)
    if k < 2:
        return 1.0
    return max(0.0, min(1.0, (k * max(probs) - 1) / (k - 1)))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DecisionClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._jev: Any = None
        self._jev_loop: asyncio.AbstractEventLoop | None = None  # the SDK's HTTP pool is loop-bound
        self._sem: asyncio.Semaphore | None = None
        self._cache: OrderedDict[str, JudgmentAnswers] = OrderedDict()
        self._laya = _Laya()

    # -- availability ---------------------------------------------------------------
    @property
    def jev_configured(self) -> bool:
        return bool(self.settings.typesafe_api_key)

    @property
    def laya_enabled(self) -> bool:
        return self.settings.laya_enabled and self._laya.importable() and self._laya.load_error is None

    def warm_laya(self) -> float | None:
        """Load Laya now (blocking). Returns load seconds, or None if disabled/unavailable."""
        if not (self.settings.laya_enabled and self._laya.importable()):
            return None
        self._laya.warm()
        return self._laya.load_seconds

    def _jev_client(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._jev is None or self._jev_loop is not loop:
            from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

            self._jev_loop = loop
            self._sem = asyncio.Semaphore(JEV_MAX_CONCURRENCY)

            self._jev = AsyncTypeSafeClient(
                api_key=self.settings.typesafe_api_key,
                model=self.settings.jev_model,
                timeout=JEV_HTTP_TIMEOUT_S,
                retry=RetryPolicy(max_retries=2, timeout=JEV_TOTAL_BUDGET_S),
            )
        return self._jev

    # -- main entry -----------------------------------------------------------------
    async def ask(self, purpose: str, state: Any, questions: dict[str, dict[str, Any]]) -> JudgmentAnswers:
        """Answer every question over one state in one request. Raises JudgeUnavailable."""
        if not questions:
            return JudgmentAnswers(model="none", backend="none")
        key = _stable_hash({"m": self.settings.jev_model, "s": state, "q": questions})
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            result = JudgmentAnswers(**{**hit.__dict__, "cached": True, "latency_ms": 0, "cost_usd": 0.0})
            self._trace(purpose, questions, result)
            return result

        errors: list[str] = []
        result: JudgmentAnswers | None = None
        if self.jev_configured:
            try:
                result = await self._ask_jev(state, questions)
            except Exception as e:  # noqa: BLE001 - any Jev failure (429, 5xx, timeout, auth) falls back
                errors.append(f"jev: {type(e).__name__}")
        else:
            errors.append("jev: TYPESAFE_API_KEY not configured")

        if result is None and self.settings.laya_enabled and self._laya.importable():
            try:
                result = await self._ask_laya(state, questions)
            except Exception as e:  # noqa: BLE001 - a broken fallback must not break the run
                errors.append(f"laya: {type(e).__name__}: {str(e)[:120]}")
        elif result is None:
            errors.append("laya: disabled or not installed")

        if result is None:
            emit_trace("judgment.call", {"purpose": purpose, "model": "unavailable", "questions": list(questions),
                                         "answers": {}, "tokens": 0, "latency_ms": 0, "cost_usd": 0.0,
                                         "error": "; ".join(errors)})
            raise JudgeUnavailable("; ".join(errors))

        self._cache[key] = result
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        self._trace(purpose, questions, result, fallback_reason="; ".join(errors) if result.degraded else "")
        return result

    def _trace(self, purpose: str, questions: dict[str, Any], r: JudgmentAnswers, fallback_reason: str = "") -> None:
        data: dict[str, Any] = {
            "purpose": purpose,
            "model": r.model,
            "questions": list(questions),
            "answers": r.compact(),
            "tokens": r.tokens,
            "latency_ms": r.latency_ms,
            "cost_usd": r.cost_usd,
            "cached": r.cached,
        }
        if r.checkpoint:
            data["checkpoint"] = r.checkpoint
        if fallback_reason:
            data["fallback_reason"] = fallback_reason
        emit_trace("judgment.call", data)

    # -- backends -------------------------------------------------------------------
    async def _ask_jev(self, state: Any, questions: dict[str, dict[str, Any]]) -> JudgmentAnswers:
        client = self._jev_client()
        sent_state = budget_state(state, JEV_STATE_CHAR_BUDGET, 8_000)
        questions = {qid: {k: v for k, v in q.items() if k != "laya"} for qid, q in questions.items()}
        t0 = time.perf_counter()
        assert self._sem is not None
        async with self._sem:
            resp = await asyncio.wait_for(
                client.system_one(state=sent_state, questions=questions, model=self.settings.jev_model),
                timeout=JEV_HARD_TIMEOUT_S,
            )
        latency = int((time.perf_counter() - t0) * 1000)
        out = JudgmentAnswers(model=resp.model, backend="jev", latency_ms=latency)
        for qid, a in resp.answers.items():
            if a.type == "noul":
                out.nouls[qid] = float(a.noul)
            elif a.type == "choice":
                out.choices[qid] = (str(a.choice), float(a.confidence), {str(k): float(v) for k, v in a.probabilities.items()})
            elif a.type == "score":
                out.scores[qid] = (float(a.score), float(a.confidence),
                                   {int(k): float(v) for k, v in a.probabilities.items()},
                                   {int(k): v for k, v in a.legend.items()})
        out.tokens = int(resp.usage.input_tokens or 0)
        out.cost_usd = round(out.tokens * self.settings.jev_price_in / 1_000_000, 8)
        return out

    async def _ask_laya(self, state: Any, questions: dict[str, dict[str, Any]]) -> JudgmentAnswers:
        t0 = time.perf_counter()
        raw, checkpoint = await asyncio.to_thread(self._laya.predict, state, questions)
        latency = int((time.perf_counter() - t0) * 1000)
        out = JudgmentAnswers(model="laya", backend="laya", checkpoint=checkpoint, latency_ms=latency,
                              tokens=int(raw["usage"].get("input_tokens") or 0), cost_usd=0.0)
        T = LAYA_TEMPERATURE
        for qid, a in raw["answers"].items():
            kind = a.get("type")
            as_noul = ((questions.get(qid) or {}).get("laya") or {}).get("as_noul")
            if kind == "choice" and as_noul:
                labels = list(a["probabilities"])
                probs = dict(zip(labels, _temper([float(a["probabilities"][k]) for k in labels], T)))
                out.nouls[qid] = probs.get(as_noul, 0.5)
                continue
            if kind == "noul":
                p = float(a["noul"])
                out.nouls[qid] = _temper([1 - p, p], T)[1]
            elif kind == "choice":
                labels = list(a["probabilities"])
                probs = _temper([float(a["probabilities"][k]) for k in labels], T)
                dist = dict(zip(labels, probs))
                out.choices[qid] = (max(dist, key=dist.get), _peak_confidence(probs), dist)  # type: ignore[arg-type]
            elif kind == "score":
                levels = sorted(int(k) for k in a["probabilities"])
                probs = _temper([float(a["probabilities"][str(k)]) for k in levels], T)
                score = sum(lv * p for lv, p in zip(levels, probs))
                legend = {int(k): v for k, v in (a.get("legend") or {}).items()}
                out.scores[qid] = (score, _peak_confidence(probs), dict(zip(levels, probs)), legend)
        return out
