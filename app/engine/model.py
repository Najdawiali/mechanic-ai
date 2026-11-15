"""The engine's model calls: JSON in, JSON out, every call traced.

The engine never talks to a provider directly. It asks a `ModelClient` for JSON that fits a schema,
for a purpose ("intake", "wording", ...), and the client records what was sent, what came back,
how long it took and what it cost. The trace is stored with the session so that "report a problem"
can be looked into (handoff 7.1, observability).

`GatewayModel` uses the LLM gateway (any OpenAI-compatible provider). Tests use `ScriptedModel`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app.llm.gateway import LLMGateway, Role


class ModelError(RuntimeError):
    """The model did not return usable JSON after a retry."""


@dataclass
class TraceEntry:
    purpose: str
    model: str
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    request: dict = field(default_factory=dict)
    output: str = ""
    error: str | None = None  # the call failed: no usable answer
    rejected: str | None = None  # the answer came back but failed a check in code, and was asked again or replaced


def reject(model: "ModelClient", reason: str) -> None:
    """Mark the model's last call as rejected by a check, so traces and the evaluation show retries and why."""
    if model.trace:
        model.trace[-1].rejected = reason


class ModelClient(Protocol):
    trace: list[TraceEntry]

    def json(self, purpose: str, *, role: Role, system: str, user: str | list[dict], schema: dict,
             max_tokens: int = 800) -> dict: ...

    def embed_query(self, text: str) -> list[float] | None: ...


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_json(text: str) -> dict:
    data = json.loads(_FENCE.sub("", text))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _short(content: str | list[dict]) -> Any:
    """What goes into the trace: images are replaced by a note, so traces stay small."""
    if isinstance(content, str):
        return content
    return [p if p.get("type") == "text" else {"type": p.get("type"), "note": "image not stored in the trace"} for p in content]


@dataclass
class GatewayModel:
    gateway: LLMGateway
    trace: list[TraceEntry] = field(default_factory=list)

    def json(self, purpose: str, *, role: Role, system: str, user: str | list[dict], schema: dict,
             max_tokens: int = 800) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error = None
        for _ in range(2):
            start = time.perf_counter()
            try:
                result = self.gateway.chat(messages, role=role, json_schema=schema, max_tokens=max_tokens)
            except Exception as exc:  # noqa: BLE001  the provider failed: traced, then retried once
                last_error = f"{type(exc).__name__}: {exc}"
                self.trace.append(TraceEntry(purpose, self.gateway.model_for(role), time.perf_counter() - start,
                                             request={"system": system, "user": _short(user)}, error=last_error))
                continue
            entry = TraceEntry(purpose, result.model, result.latency_s, result.prompt_tokens, result.completion_tokens,
                               result.cost_usd, {"system": system, "user": _short(user)}, result.text)
            self.trace.append(entry)
            try:
                return parse_json(result.text)
            except ValueError as exc:
                entry.error = last_error = f"not JSON: {exc}"
        raise ModelError(f"{purpose}: {last_error}")

    def embed_query(self, text: str) -> list[float] | None:
        try:
            return self.gateway.embed([text], query=True)[0]
        except Exception:  # noqa: BLE001  search by words still works without the vector
            return None


@dataclass
class ScriptedModel:
    """For tests: `handlers[purpose](system, user, schema) -> dict`. A missing purpose is an error."""

    handlers: dict[str, Callable[[str, Any, dict], dict]]
    trace: list[TraceEntry] = field(default_factory=list)
    vectors: Callable[[str], list[float] | None] | None = None

    def json(self, purpose: str, *, role: Role, system: str, user: str | list[dict], schema: dict,
             max_tokens: int = 800) -> dict:
        if purpose not in self.handlers:
            raise ModelError(f"no scripted answer for {purpose!r}")
        out = self.handlers[purpose](system, user, schema)
        self.trace.append(TraceEntry(purpose, "scripted", 0.0, request={"system": system, "user": _short(user)},
                                     output=json.dumps(out, ensure_ascii=False)))
        return out

    def embed_query(self, text: str) -> list[float] | None:
        return self.vectors(text) if self.vectors else None
