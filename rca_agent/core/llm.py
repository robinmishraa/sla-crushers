"""LLM client with dual-provider support.

The agent is built around Anthropic's tool-use semantics (`tool_use` content blocks,
`input_schema` tool specs, `tool_result` user-content blocks). For the hackathon
we also support OpenRouter (which is OpenAI-compatible) so a `sk-or-...` key works.

When the OpenRouter path is active, this module translates between the two formats
so the orchestrator code stays untouched — `call_messages()` always returns an
Anthropic-shaped response object with `.content` being a list of objects with
`.type` in {"text", "tool_use"} and the usual `.id` / `.name` / `.input` fields.

This is deliberately a thin shim: just enough fidelity to make the existing
orchestrator work. It does NOT try to be a general-purpose adapter.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from rca_agent.core.settings import settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


# ---------- Prompt loader ----------

@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {p}")
    return p.read_text(encoding="utf-8")


# ---------- Anthropic-compat duck types for the OpenRouter path ----------

class _Block:
    """Minimal stand-in for an anthropic.types.* content block."""
    __slots__ = ("type", "text", "id", "name", "input")

    def __init__(self, type: str, **kw: Any) -> None:
        self.type = type
        # Only set the fields the orchestrator actually reads.
        self.text = kw.get("text", "")
        self.id = kw.get("id", "")
        self.name = kw.get("name", "")
        self.input = kw.get("input", {})


class _Response:
    """Minimal stand-in for anthropic.types.Message. Only `.content` is used."""
    __slots__ = ("content",)

    def __init__(self, content: list[_Block]) -> None:
        self.content = content


# ---------- Client construction ----------

@lru_cache(maxsize=1)
def get_client() -> Any:
    s = settings()
    if not s.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    if s.LLM_PROVIDER == "openrouter":
        from openai import OpenAI
        return OpenAI(
            api_key=s.ANTHROPIC_API_KEY,
            base_url="https://openrouter.ai/api/v1",
        )
    import anthropic
    return anthropic.Anthropic(api_key=s.ANTHROPIC_API_KEY)


# ---------- Public entry point ----------

def call_messages(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> Any:
    """Send a message + tools to the configured LLM.

    Always returns an object with `.content` being a list of duck-typed blocks
    where `block.type` ∈ {"text", "tool_use"} and `tool_use` blocks carry
    `id`, `name`, `input` (dict). The orchestrator depends on this shape.
    """
    if settings().LLM_PROVIDER == "openrouter":
        return _call_openrouter(system=system, messages=messages, tools=tools or [],
                                max_tokens=max_tokens, temperature=temperature)
    return _call_anthropic(system=system, messages=messages, tools=tools or [],
                           max_tokens=max_tokens, temperature=temperature)


# ---------- Anthropic direct ----------

def _call_anthropic(*, system, messages, tools, max_tokens, temperature):
    return get_client().messages.create(
        model=settings().LLM_MODEL,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ---------- OpenRouter (OpenAI-compatible) ----------

# OpenRouter routes to Anthropic / Bedrock / Vertex AI, all of which require
# tool names to match `^[a-zA-Z0-9_-]+$`. Our names use dots (e.g. "slack.get_thread"),
# so we rewrite "." → "__" on the way out and "__" → "." on the way back. Two
# underscores survives a single round-trip because none of our native tool names
# contain a double underscore.
_DOT_SUB = "__"


def _sanitize_tool_name(name: str) -> str:
    return name.replace(".", _DOT_SUB)


def _desanitize_tool_name(name: str) -> str:
    return name.replace(_DOT_SUB, ".")


def _call_openrouter(*, system, messages, tools, max_tokens, temperature):
    oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        for converted in _msg_anthropic_to_openai(m):
            oai_messages.append(converted)

    oai_tools = [_tool_anthropic_to_openai(t) for t in tools] if tools else None

    kwargs: dict[str, Any] = {
        "model": settings().LLM_MODEL,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if oai_tools:
        kwargs["tools"] = oai_tools
        kwargs["tool_choice"] = "auto"

    resp = get_client().chat.completions.create(**kwargs)
    return _openai_to_anthropic_response(resp)


def _msg_anthropic_to_openai(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one Anthropic-format message to one-or-more OpenAI-format messages."""
    role = m.get("role", "user")
    content = m.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content or "")}]

    if role == "user":
        # Anthropic mixes plain text and tool_result blocks in one user message.
        # OpenAI wants tool results as separate messages with role="tool".
        text_parts: list[str] = []
        tool_msgs: list[dict[str, Any]] = []
        for b in content:
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text", ""))
            elif btype == "tool_result":
                payload = b.get("content", "")
                if not isinstance(payload, str):
                    try:
                        payload = json.dumps(payload, default=str)
                    except Exception:
                        payload = str(payload)
                tool_msgs.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id", ""),
                    "content": payload,
                })
        out: list[dict[str, Any]] = []
        if tool_msgs:
            # OpenAI requires tool messages to come right after the assistant message
            # that produced the tool_calls — they shouldn't be wrapped in a "user" text.
            out.extend(tool_msgs)
        if text_parts:
            out.append({"role": "user", "content": "\n".join(text_parts)})
        return out or [{"role": "user", "content": ""}]

    if role == "assistant":
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for b in content:
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": _sanitize_tool_name(b.get("name", "")),
                        "arguments": json.dumps(b.get("input", {}), default=str),
                    },
                })
        msg: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            msg["content"] = "\n".join(text_parts)
        else:
            msg["content"] = None  # OpenAI accepts null when there are tool_calls
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return [msg]

    # Fallback for any other role (system handled outside this fn)
    return [{"role": role, "content": str(content)}]


def _tool_anthropic_to_openai(t: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic tool spec to OpenAI function-tool spec."""
    return {
        "type": "function",
        "function": {
            "name": _sanitize_tool_name(t["name"]),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _openai_to_anthropic_response(resp: Any) -> _Response:
    """Wrap an OpenAI ChatCompletion as an Anthropic-shaped response."""
    if not getattr(resp, "choices", None):
        return _Response(content=[])
    msg = resp.choices[0].message
    blocks: list[_Block] = []
    text = getattr(msg, "content", None)
    if text:
        blocks.append(_Block(type="text", text=text))
    for tc in (getattr(msg, "tool_calls", None) or []):
        raw_args = getattr(tc.function, "arguments", "") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            args = {"_raw_arguments": raw_args}
        blocks.append(_Block(
            type="tool_use",
            id=tc.id,
            name=_desanitize_tool_name(tc.function.name),
            input=args if isinstance(args, dict) else {"_value": args},
        ))
    return _Response(content=blocks)
