"""
LLM abstraction layer.  All provider-specific imports are local to each class;
nothing outside this file may import openai or anthropic directly.

Protocol:
  chat(messages, system) -> str
  chat_with_tools(messages, system, tools, image, image_mime) -> LLMResponse   (single pass)
  run_tool_loop(messages, system, tools, execute, ...) -> LoopResult            (agentic loop)

Tool definitions are in neutral JSON Schema format (tools.py).
Each provider translates to its own wire format internally.
Image bytes (if provided) are injected into the last user message in the
provider's multimodal format; history messages remain plain text.

run_tool_loop is the real agentic loop: it calls the model, runs any tools it
asks for via the `execute` callback, feeds the results back, and repeats until
the model produces a final answer (or max_iters is hit). This is what lets the
bot CHAIN tools — e.g. search Gmail for ids, then fetch each message's body.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Callback the loop uses to run one tool: (name, args) -> human-readable result string.
ExecuteFn = Callable[[str, dict], Awaitable[str]]


@dataclass
class ToolCall:
    name: str
    args: dict
    call_id: str = ""


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class LoopResult:
    text: str                                  # final natural-language answer
    tool_calls_made: int = 0
    last_results: list[str] = field(default_factory=list)  # fallback if text is empty


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
    async def run_tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        execute: ExecuteFn,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
        max_iters: int = 6,
    ) -> LoopResult: ...


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

    async def run_tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        execute: ExecuteFn,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
        max_iters: int = 6,
    ) -> LoopResult:
        base = self._inject_image(messages, image, image_mime) if image else messages
        convo: list[dict] = [{"role": "system", "content": system}] + [dict(m) for m in base]
        oa_tools = self._to_openai_tools(tools)
        made = 0
        last_results: list[str] = []

        for _ in range(max_iters):
            resp = await self._client.chat.completions.create(
                model=self.chat_model, messages=convo, tools=oa_tools, tool_choice="auto"
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return LoopResult((msg.content or "").strip(), made, last_results)

            convo.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            last_results = []
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await execute(tc.function.name, args)
                made += 1
                last_results.append(result)
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        # Ran out of iterations — get a final answer without offering more tools.
        resp = await self._client.chat.completions.create(model=self.chat_model, messages=convo)
        return LoopResult((resp.choices[0].message.content or "").strip(), made, last_results)


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

    async def run_tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        execute: ExecuteFn,
        image: bytes | None = None,
        image_mime: str = "image/jpeg",
        max_iters: int = 6,
    ) -> LoopResult:
        msgs: list[dict] = [
            dict(m)
            for m in (self._inject_image(messages, image, image_mime) if image else messages)
        ]
        an_tools = self._to_anthropic_tools(tools)
        made = 0
        last_results: list[str] = []

        def _text(content) -> str:
            return "".join(
                b.text for b in content if getattr(b, "type", None) == "text"
            ).strip()

        for _ in range(max_iters):
            resp = await self._client.messages.create(
                model=self.chat_model,
                max_tokens=4096,
                system=system,
                messages=msgs,
                tools=an_tools,
            )
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                return LoopResult(_text(resp.content), made, last_results)

            msgs.append({"role": "assistant", "content": resp.content})
            last_results = []
            tool_results = []
            for tu in tool_uses:
                result = await execute(tu.name, tu.input)
                made += 1
                last_results.append(result)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": result}
                )
            msgs.append({"role": "user", "content": tool_results})

        resp = await self._client.messages.create(
            model=self.chat_model, max_tokens=4096, system=system, messages=msgs
        )
        return LoopResult(_text(resp.content), made, last_results)


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
