"""System 2 client: Meta Muse Spark 1.3 Contributor via the OpenAI-compatible Meta Model API.

Every generative step in Adjutant (intent parsing, planning, re-planning, reflection,
drafting, argument repair, memory extraction) goes through `MuseClient.complete`.

Notes on the Meta Model API (Chat Completions surface):
  * base_url https://api.meta.ai/v1, model `muse-spark-1.3-contributor`
  * reasoning model: `reasoning_effort` in {minimal, low, medium, high, xhigh}
  * `max_completion_tokens` (not max_tokens); `stream`, `stop`, `n>1`, `logprobs` are rejected
  * structured output via response_format={"type": "json_schema", ...}
  * Contributor tier: 100 RPM -> we keep a small concurrency semaphore + retry on 429
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..core.tracing import emit_trace


@dataclass
class MuseResult:
    text: str
    data: Any
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float
    model: str


class LLMUnavailable(RuntimeError):
    pass


# Test hook: a callable(messages, purpose, schema) -> str|dict used instead of the API.
FakeResponder = Callable[[list[dict[str, Any]], str, dict[str, Any] | None], Any]
_fake_responder: FakeResponder | None = None


def set_fake_responder(fn: FakeResponder | None) -> None:
    global _fake_responder
    _fake_responder = fn


def _extract_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start >= 0:
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            return json.loads(text[start : end + 1])
    raise ValueError("model did not return JSON")


class MuseClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        self._sem = asyncio.Semaphore(6)

    @property
    def model(self) -> str:
        return self.settings.model_name

    @property
    def available(self) -> bool:
        return bool(self.settings.model_api_key) or _fake_responder is not None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            if not self.settings.model_api_key:
                raise LLMUnavailable("MODEL_API_KEY is not configured")
            self._client = AsyncOpenAI(
                api_key=self.settings.model_api_key,
                base_url=self.settings.model_base_url,
                timeout=120,
                max_retries=0,  # we retry ourselves (429 handling + accounting)
            )
        return self._client

    def _cost(self, tin: int, tout: int) -> float:
        s = self.settings
        return (tin * s.model_price_in + tout * s.model_price_out) / 1_000_000

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: str,
        schema: dict[str, Any] | None = None,
        effort: str = "low",
        max_tokens: int = 4000,
    ) -> MuseResult:
        t0 = time.perf_counter()
        if _fake_responder is not None:
            out = _fake_responder(messages, purpose, schema)
            text = out if isinstance(out, str) else json.dumps(out)
            data = (_extract_json(text) if schema else None)
            result = MuseResult(text, data, 0, 0, int((time.perf_counter() - t0) * 1000), 0.0, "fake")
            emit_trace("llm.call", {"purpose": purpose, "model": "fake", "effort": effort, "input_tokens": 0,
                                    "output_tokens": 0, "latency_ms": result.latency_ms, "cost_usd": 0.0})
            return result

        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.settings.model_name,
            "messages": messages,
            "reasoning_effort": effort,
            "max_completion_tokens": max_tokens,
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": re.sub(r"[^a-zA-Z0-9_]", "_", purpose)[:60] or "out", "schema": schema},
            }

        last_err: Exception | None = None
        for attempt in range(4):
            try:
                async with self._sem:
                    resp = await client.chat.completions.create(**kwargs)
                break
            except Exception as e:  # openai.RateLimitError, APIStatusError, APIConnectionError...
                last_err = e
                status = getattr(e, "status_code", None)
                msg = str(e)
                # Schema rejected by the server -> fall back to prompt-only JSON once.
                if status == 400 and "response_format" in kwargs and ("schema" in msg or "response_format" in msg):
                    kwargs.pop("response_format")
                    kwargs["messages"] = messages + [
                        {"role": "user", "content": "Respond with only a JSON object matching this JSON Schema:\n"
                         + json.dumps(schema)}
                    ]
                    continue
                if status in (429, 500, 502, 503, 504, 529) or status is None:
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise LLMUnavailable(f"Muse call failed ({status}): {msg[:300]}") from e
        else:
            raise LLMUnavailable(f"Muse call failed after retries: {str(last_err)[:300]}")

        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = resp.usage
        tin = getattr(usage, "prompt_tokens", 0) or 0
        tout = getattr(usage, "completion_tokens", 0) or 0
        data = None
        if schema is not None:
            try:
                data = _extract_json(text)
            except (ValueError, json.JSONDecodeError) as e:
                raise LLMUnavailable(f"Muse returned non-JSON for {purpose}: {text[:200]}") from e
        result = MuseResult(
            text=text,
            data=data,
            input_tokens=tin,
            output_tokens=tout,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            cost_usd=self._cost(tin, tout),
            model=resp.model or self.settings.model_name,
        )
        emit_trace("llm.call", {
            "purpose": purpose, "model": result.model, "effort": effort,
            "input_tokens": tin, "output_tokens": tout,
            "latency_ms": result.latency_ms, "cost_usd": round(result.cost_usd, 6),
        })
        return result


_muse: MuseClient | None = None


def get_llm() -> MuseClient:
    global _muse
    if _muse is None:
        _muse = MuseClient()
    return _muse
