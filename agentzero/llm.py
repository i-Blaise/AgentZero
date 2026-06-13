"""
LLM abstraction layer.  All provider-specific imports are local to each class;
nothing outside this file may import openai or anthropic directly.

Protocol:
  chat(messages, system) -> str
  chat_with_tools(messages, system, tools, image, image_mime) -> LLMResponse

Tool definitions are in neutral JSON Schema format (tools.py).
Each provider translates to its own wire format internally.
Image bytes (if provided) are injected into the last user message in the
provider's multimodal format; history messages remain plain text.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ToolCall:
    name: str
    args: dict
    call_id: str = ""


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


@runtime_checkable
class LLMProvider(Protocol):
    async def chat(self, messages: list[dict], system: str) -> str: ...
    async def chat_with_tools(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------

class OpenAIProvider:
    def __init__(self, chat_model: str, digest_model: str, api_key: str) -> None:
        import openai
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self.chat_model = chat_model
        self.digest_model = digest_model

    def _to_openai_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

    def _inject_image(
        self, messages: list[dict], image: bytes, image_mime: str
    ) -> list[dict]:
        """Return a copy of messages with the image embedded in the last user turn."""
        msgs = [m.copy() for m in messages]
        for i in reversed(range(len(msgs))):
            if msgs[i]["role"] == "user":
                b64 = base64.b64encode(image).decode()
                text = msgs[i]["content"]
                msgs[i] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image_mime};base64,{b64}",
                                "detail": "auto",
                            },
                        },
                    ],
                }
                break
        return msgs

    async def chat(self, messages: list[dict], system: str) -> str:
        resp = await self._client.chat.completions.create(
            model=self.digest_model,
            messages=[{"role": "system", "content": system}] + messages,
        )
        return resp.choices[0].message.content or ""

    async def chat_with_tools(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> LLMResponse:
        msgs = self._inject_image(messages, image, image_mime) if image else messages
        resp = await self._client.chat.completions.create(
            model=self.chat_model,
            messages=[{"role": "system", "content": system}] + msgs,
            tools=self._to_openai_tools(tools),
            tool_choice="auto",
        )
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                calls.append(
                    ToolCall(
                        name=tc.function.name,
                        args=json.loads(tc.function.arguments),
                        call_id=tc.id,
                    )
                )
        return LLMResponse(content=msg.content, tool_calls=calls)


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider:
    def __init__(self, chat_model: str, digest_model: str, api_key: str) -> None:
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.chat_model = chat_model
        self.digest_model = digest_model

    def _to_anthropic_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

    def _inject_image(
        self, messages: list[dict], image: bytes, image_mime: str
    ) -> list[dict]:
        msgs = [m.copy() for m in messages]
        for i in reversed(range(len(msgs))):
            if msgs[i]["role"] == "user":
                b64 = base64.b64encode(image).decode()
                text = msgs[i]["content"]
                msgs[i] = {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": image_mime,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": text},
                    ],
                }
                break
        return msgs

    async def chat(self, messages: list[dict], system: str) -> str:
        resp = await self._client.messages.create(
            model=self.digest_model,
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        for block in resp.content:
            if block.type == "text":
                return block.text
        return ""

    async def chat_with_tools(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> LLMResponse:
        msgs = self._inject_image(messages, image, image_mime) if image else messages
        resp = await self._client.messages.create(
            model=self.chat_model,
            max_tokens=1024,
            system=system,
            messages=msgs,
            tools=self._to_anthropic_tools(tools),
        )
        text: str | None = None
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text = block.text
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(name=block.name, args=block.input, call_id=block.id)
                )
        return LLMResponse(content=text, tool_calls=calls)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_provider: OpenAIProvider | AnthropicProvider | None = None


def get_provider() -> OpenAIProvider | AnthropicProvider:
    global _provider
    if _provider is not None:
        return _provider
    from agentzero.config import (
        LLM_PROVIDER,
        OPENAI_API_KEY,
        OPENAI_CHAT_MODEL,
        OPENAI_DIGEST_MODEL,
        ANTHROPIC_API_KEY,
        ANTHROPIC_CHAT_MODEL,
        ANTHROPIC_DIGEST_MODEL,
    )
    if LLM_PROVIDER == "anthropic":
        _provider = AnthropicProvider(
            chat_model=ANTHROPIC_CHAT_MODEL,
            digest_model=ANTHROPIC_DIGEST_MODEL,
            api_key=ANTHROPIC_API_KEY,
        )
    else:
        _provider = OpenAIProvider(
            chat_model=OPENAI_CHAT_MODEL,
            digest_model=OPENAI_DIGEST_MODEL,
            api_key=OPENAI_API_KEY,
        )
    return _provider
