"""Model-agnostic LLM gateway (team requirement 17: no lock-in to one provider).

The rest of the service asks for a ROLE ("reasoning", "fast", "vision").
Which model serves a role is configuration (.env), so changing model or
provider never touches the engine code.

Any OpenAI-compatible endpoint works: DeepInfra today, another host or a
self-hosted vLLM server later.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import OpenAI

from app.config import Settings, get_settings

Role = Literal["reasoning", "fast", "vision"]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] | None  # None = the model returned arguments that are not valid JSON
    raw_arguments: str


@dataclass
class LLMResult:
    model: str
    text: str
    reasoning: str | None  # thinking text, if the provider returns it separately
    tool_calls: list[ToolCall]
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None  # as reported by the provider; None if it does not report one
    latency_s: float


@dataclass
class StreamResult:
    model: str
    text: str
    ttft_s: float | None  # time to the first visible token
    latency_s: float


@dataclass
class LLMGateway:
    settings: Settings = field(default_factory=get_settings)
    http_client: httpx.Client | None = None  # tests inject a fake transport here

    def __post_init__(self) -> None:
        key = self.settings.deepinfra_api_key
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError(
                "DEEPINFRA_API_KEY is not set. Copy .env.example to .env and put the key there."
            )
        self.client = OpenAI(
            api_key=key.get_secret_value().strip(),
            base_url=self.settings.llm_base_url,
            timeout=self.settings.llm_timeout_s,
            max_retries=1,
            http_client=self.http_client,
        )

    # -- routing ---------------------------------------------------------
    def model_for(self, role: Role) -> str:
        return {
            "reasoning": self.settings.llm_reasoning_model,
            "fast": self.settings.llm_fast_model,
            "vision": self.settings.llm_vision_model,
        }[role]

    def list_models(self) -> list[str]:
        return [m.id for m in self.client.models.list()]

    # -- calls -----------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        role: Role = "reasoning",
        model: str | None = None,  # explicit override, used by the evaluation to compare models
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        kwargs = self._kwargs(messages, role, model, json_mode, json_schema, tools, tool_choice,
                              temperature, max_tokens)
        start = time.perf_counter()
        resp = self.client.chat.completions.create(**kwargs)
        latency = time.perf_counter() - start

        choice = resp.choices[0]
        msg = choice.message
        extra = msg.model_extra or {}
        usage = resp.usage
        usage_extra = (usage.model_extra or {}) if usage else {}

        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or ""
            try:
                parsed = json.loads(raw)
                args = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                args = None
            calls.append(ToolCall(name=tc.function.name, arguments=args, raw_arguments=raw))

        return LLMResult(
            model=kwargs["model"],
            text=msg.content or "",
            reasoning=extra.get("reasoning_content") or extra.get("reasoning"),
            tool_calls=calls,
            finish_reason=choice.finish_reason,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cost_usd=usage_extra.get("estimated_cost"),
            latency_s=latency,
        )

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        role: Role = "reasoning",
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> StreamResult:
        kwargs = self._kwargs(messages, role, model, False, None, None, None, temperature, max_tokens)
        start = time.perf_counter()
        ttft: float | None = None
        parts: list[str] = []
        for chunk in self.client.chat.completions.create(stream=True, **kwargs):
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content
            if piece:
                if ttft is None:
                    ttft = time.perf_counter() - start
                parts.append(piece)
        return StreamResult(model=kwargs["model"], text="".join(parts), ttft_s=ttft,
                            latency_s=time.perf_counter() - start)

    def _kwargs(self, messages, role, model, json_mode, json_schema, tools, tool_choice,
                temperature, max_tokens) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model or self.model_for(role),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": json_schema},
            }
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        return kwargs


# -- message helpers -------------------------------------------------------
def image_part_from_bytes(data: bytes, mime: str = "image/png") -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def image_part_from_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return image_part_from_bytes(path.read_bytes(), mime)


def user_message_with_image(image_part: dict[str, Any], text: str) -> dict[str, Any]:
    # DeepInfra's Gemma 4 guide: put the image BEFORE the text in the message.
    return {"role": "user", "content": [image_part, {"type": "text", "text": text}]}
