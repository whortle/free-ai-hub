from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, Iterator


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "".join(parts)
    return ""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _usage(chat: dict[str, Any]) -> tuple[int, int, int]:
    usage = chat.get("usage") if isinstance(chat, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or input_tokens + output_tokens)
    return input_tokens, output_tokens, total


def _message(chat: dict[str, Any]) -> dict[str, Any]:
    choices = chat.get("choices") if isinstance(chat, dict) else None
    if not isinstance(choices, list) or not choices:
        return {}
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    return message if isinstance(message, dict) else {}


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return ""


def _response_text(chat: dict[str, Any]) -> str:
    message = _message(chat)
    return _content_text(message) or str(message.get("reasoning") or "")


def _finish_reason(chat: dict[str, Any]) -> str:
    choices = chat.get("choices") if isinstance(chat, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


# --------------------------------------------------------------- Responses API
def _responses_tools(tools: Any) -> list[dict[str, Any]] | None:
    if not isinstance(tools, list):
        return None
    converted = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if function is None:
            function = {
                "name": tool.get("name"),
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            }
        if function.get("name"):
            converted.append({"type": "function", "function": function})
    return converted or None


def _responses_tool_choice(choice: Any) -> Any:
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        function = choice.get("function")
        name = choice.get("name") or (function.get("name") if isinstance(function, dict) else None)
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    model = str(body.get("model") or "auto")
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": _text(instructions)})
    source = body.get("input")
    if isinstance(source, str):
        messages.append({"role": "user", "content": source})
    elif isinstance(source, list):
        for item in source:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            text = _text(item.get("content"))
            if text:
                messages.append({"role": role, "content": text})
    if not messages:
        raise ValueError("input is required")

    chat: dict[str, Any] = {"model": model, "messages": messages}
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed", "user", "stop"):
        if key in body:
            chat[key] = body[key]
    if "stream" in body:
        chat["stream"] = bool(body["stream"])
    tools = _responses_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    tool_choice = _responses_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        chat["tool_choice"] = tool_choice
    return chat


def chat_to_responses(chat: dict[str, Any], requested_model: str) -> dict[str, Any]:
    message = _message(chat)
    text = _content_text(message) or str(message.get("reasoning") or "")
    output: list[dict[str, Any]] = []
    if text:
        output.append(
            {
                "type": "message",
                "id": _new_id("msg"),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for tool in message.get("tool_calls") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        output.append(
            {
                "type": "function_call",
                "id": _new_id("fc"),
                "call_id": str(tool.get("id") or _new_id("call")),
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or "{}"),
                "status": "completed",
            }
        )
    if not output:
        output.append(
            {
                "type": "message",
                "id": _new_id("msg"),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
        )
    input_tokens, output_tokens, total = _usage(chat)
    return {
        "id": _new_id("resp"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": requested_model,
        "output": output,
        "output_text": text,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": total,
        },
    }


# ------------------------------------------------------------- Anthropic API
def _anthropic_tools(tools: Any) -> list[dict[str, Any]] | None:
    if not isinstance(tools, list):
        return None
    converted = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return converted or None


def _anthropic_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def messages_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    model = str(body.get("model") or "auto")
    messages: list[dict[str, Any]] = []
    system = _text(body.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("messages is required")
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user")
        if role not in {"user", "assistant"}:
            role = "user"
        content = raw.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_result":
                    parts.append(_text(block.get("content")))
            text = "\n".join(part for part in parts if part)
        else:
            text = ""
        messages.append({"role": role, "content": text})
    chat: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(body.get("max_tokens") or 1024),
    }
    for key in ("temperature", "top_p", "stop"):
        if key in body:
            chat["stop" if key == "stop_sequences" else key] = body[key]
    if "stop_sequences" in body:
        chat["stop"] = body["stop_sequences"]
    if "stream" in body:
        chat["stream"] = bool(body["stream"])
    tools = _anthropic_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    tool_choice = _anthropic_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        chat["tool_choice"] = tool_choice
    return chat


def chat_to_messages(chat: dict[str, Any], requested_model: str) -> dict[str, Any]:
    message = _message(chat)
    text = _content_text(message) or str(message.get("reasoning") or "")
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for tool in message.get("tool_calls") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            arguments = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(tool.get("id") or _new_id("toolu")),
                "name": str(function.get("name") or ""),
                "input": arguments,
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    finish = _finish_reason(chat)
    stop_reason = "tool_use" if any(block["type"] == "tool_use" for block in blocks) else ("max_tokens" if finish == "length" else "end_turn")
    input_tokens, output_tokens, _total = _usage(chat)
    return {
        "id": _new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


# ------------------------------------------------------------------ streaming
def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def iter_chat_events(raw_iter: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    buffer = ""
    for chunk in raw_iter:
        buffer += chunk.decode("utf-8", errors="ignore")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                yield {"type": "done"}
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield {"type": "chunk", "data": event}


def _event_delta(event: dict[str, Any]) -> tuple[str, str]:
    choices = event.get("choices") if isinstance(event, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return "", ""
    content = delta.get("content")
    reasoning = delta.get("reasoning")
    return (
        content if isinstance(content, str) else "",
        reasoning if isinstance(reasoning, str) else "",
    )


def responses_stream(raw_iter: Iterable[bytes], requested_model: str) -> Iterator[str]:
    response_id = _new_id("resp")
    item_id = _new_id("msg")
    created = int(time.time())
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    seq = 0
    skeleton = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": requested_model,
        "output": [],
    }
    yield _sse("response.created", {"type": "response.created", "response": skeleton, "sequence_number": seq})
    seq += 1
    yield _sse(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            "sequence_number": seq,
        },
    )
    seq += 1
    yield _sse(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
            "sequence_number": seq,
        },
    )
    seq += 1
    for event in iter_chat_events(raw_iter):
        if event["type"] != "chunk":
            continue
        data = event["data"]
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        content, reasoning = _event_delta(data)
        delta = content or reasoning
        if delta:
            text_parts.append(delta)
            yield _sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": delta,
                    "sequence_number": seq,
                },
            )
            seq += 1
    text = "".join(text_parts)
    yield _sse(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
            "sequence_number": seq,
        },
    )
    seq += 1
    yield _sse(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
            "sequence_number": seq,
        },
    )
    seq += 1
    output_item = {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    yield _sse(
        "response.output_item.done",
        {"type": "response.output_item.done", "output_index": 0, "item": output_item, "sequence_number": seq},
    )
    seq += 1
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    final = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "completed",
        "model": requested_model,
        "output": [output_item],
        "output_text": text,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": input_tokens + output_tokens,
        },
    }
    yield _sse("response.completed", {"type": "response.completed", "response": final, "sequence_number": seq})


def messages_stream(raw_iter: Iterable[bytes], requested_model: str) -> Iterator[str]:
    message_id = _new_id("msg")
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    text_parts: list[str] = []
    for event in iter_chat_events(raw_iter):
        if event["type"] != "chunk":
            continue
        data = event["data"]
        content, reasoning = _event_delta(data)
        delta = content or reasoning
        if delta:
            text_parts.append(delta)
            yield _sse(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}},
            )
    text = "".join(text_parts)
    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": max(1, len(text) // 4)},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})
