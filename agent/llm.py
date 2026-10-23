"""One OpenAI-compatible client path for both Gemini and Groq, with retry + failover."""
from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass, field

from openai import (
    OpenAI, RateLimitError, APIStatusError, APIError,
)

from . import config

log = logging.getLogger("agent.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""


def assistant_message(resp: LLMResponse) -> dict:
    """Plain OpenAI-format dict, so history is portable across providers."""
    msg: dict = {"role": "assistant", "content": resp.content or ""}
    if resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in resp.tool_calls
        ]
    return msg


class LLMClient:
    def __init__(self, order: list[str] | None = None):
        names = order or [config.PRIMARY, config.FALLBACK]
        self.providers = []
        for name in names:
            if not name or name not in config.PROVIDERS:
                continue
            cfg = config.PROVIDERS[name]
            key = os.getenv(cfg["api_key_env"])
            if not key:
                log.warning("No %s set; skipping provider '%s'", cfg["api_key_env"], name)
                continue
            client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=60, max_retries=0)
            model_list = cfg.get("models") or [cfg["model"]]
            seen = set()
            for m in model_list:
                if m and m not in seen:
                    seen.add(m)
                    self.providers.append((f"{name}:{m}", client, m))
        if not self.providers:
            raise RuntimeError("No LLM provider configured. Set GEMINI_API_KEY and/or GROQ_API_KEY in .env.")

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        last_err: Exception | None = None
        step_delay = getattr(config, "STEP_DELAY_SECONDS", 0)
        if step_delay > 0:
            time.sleep(step_delay)

        # Pass 1: Immediately try each available model across providers without long waits
        # Pass 2: If all models are rate-limited, wait with backoff and retry
        for round_idx in range(getattr(config, "RETRIES_PER_PROVIDER", 2)):
            if round_idx > 0:
                wait_sec = 8 * round_idx
                log.warning("All models rate-limited; cooling down %ss before round %s...", wait_sec, round_idx + 1)
                time.sleep(wait_sec)

            for name, client, model in self.providers:
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        temperature=config.TEMPERATURE,
                    )
                    return self._normalize(resp, name)
                except RateLimitError as e:
                    last_err = e
                    log.warning("%s hit rate limit (429); switching immediately to next model", name)
                    continue
                except APIStatusError as e:
                    last_err = e
                    log.warning("%s HTTP %s; switching to next model", name, e.status_code)
                    continue
                except APIError as e:
                    last_err = e
                    log.warning("%s API error (%s); switching to next model", name, e)
                    continue

        raise RuntimeError(f"All LLM providers/models exhausted: {last_err}")

    @staticmethod
    def _normalize(resp, provider: str) -> LLMResponse:
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    args = {"__parse_error__": raw}
            except json.JSONDecodeError:
                args = {"__parse_error__": raw}
            calls.append(ToolCall(id=tc.id or f"call_{len(calls)}", name=tc.function.name, arguments=args))
        return LLMResponse(content=msg.content, tool_calls=calls, provider=provider)