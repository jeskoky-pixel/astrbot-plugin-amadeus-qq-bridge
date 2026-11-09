"""Message segment serialization and rich-message construction.

AstrBot keeps the platform-neutral QQ message chain in component objects.  The
continuous runner must preserve those components when a message is queued and
must be able to send a component chain from a tool call.  This module is kept
independent from the runner so it can also be tested without a live platform.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any


def _normalise(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalise(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _normalise(value.model_dump(exclude_none=False))
        except Exception:  # pragma: no cover - compatibility fallback
            pass
    if hasattr(value, "__dict__"):
        return _normalise(vars(value))
    return value


def component_to_dict(component: Any) -> dict[str, Any]:
    """Convert an AstrBot component (or a dict) to a JSON-safe segment."""
    if isinstance(component, dict):
        result = _normalise(component)
    elif hasattr(component, "toDict"):
        result = _normalise(component.toDict())
    else:
        result = _normalise(component)
    if not isinstance(result, dict):
        return {"type": "unknown", "data": {"text": str(result)}}
    return result


def serialize_segments(segments: list[Any] | None) -> list[dict[str, Any]]:
    return [component_to_dict(segment) for segment in (segments or [])]


def segments_json(segments: list[Any] | None) -> str:
    return json.dumps(serialize_segments(segments), ensure_ascii=False, default=str)


def message_for_agent(text: str, segments: list[Any] | None) -> str:
    """Build a compact text envelope for follow-ups that AstrBot cannot carry as a chain."""
    serialised = serialize_segments(segments)
    non_plain = [item for item in serialised if item.get("type") not in {"text", "plain"}]
    if not non_plain:
        return (text or "").strip()
    payload = json.dumps(serialised, ensure_ascii=False, default=str)
    return f"{(text or '').strip()}\n[QQ富消息段]\n{payload}".strip()


def _data(spec: dict[str, Any]) -> dict[str, Any]:
    data = spec.get("data")
    if isinstance(data, dict):
        merged = dict(data)
        merged.update({k: v for k, v in spec.items() if k not in {"type", "data"}})
        return merged
    return {k: v for k, v in spec.items() if k != "type"}


def _source(data: dict[str, Any]) -> str:
    return str(data.get("url") or data.get("file") or data.get("path") or "")


def build_message_chain(text: str = "", segments: list[dict[str, Any]] | None = None) -> list[Any]:
    """Build AstrBot components from simple JSON specs accepted by the tool.

    Supported types are text/plain, image, record/audio, video, file, at,
    reply, json, forward and raw component-like dictionaries.  Unsupported
    segments are represented as text so a message is never silently lost.
    """
    from astrbot.core.message.components import (
        At,
        File,
        Forward,
        Image,
        Json,
        Plain,
        Record,
        Reply,
        Video,
    )

    chain: list[Any] = []
    if text and text.strip():
        chain.append(Plain(text))
    for raw in segments or []:
        if not isinstance(raw, dict):
            chain.append(Plain(str(raw)))
            continue
        typ = str(raw.get("type", "plain")).lower()
        data = _data(raw)
        if typ in {"text", "plain"}:
            chain.append(Plain(str(data.get("text", ""))))
        elif typ == "image":
            source = _source(data)
            if source.startswith("base64://"):
                chain.append(Image.fromBase64(source[9:]))
            elif source.startswith(("http://", "https://")):
                chain.append(Image.fromURL(source))
            else:
                chain.append(Image.fromFileSystem(source))
        elif typ in {"record", "audio", "voice"}:
            source = _source(data)
            if source.startswith("base64://"):
                chain.append(Record.fromBase64(source[9:]))
            elif source.startswith(("http://", "https://")):
                chain.append(Record.fromURL(source))
            else:
                chain.append(Record.fromFileSystem(source))
        elif typ == "video":
            source = _source(data)
            if source.startswith("base64://"):
                chain.append(Video.fromBase64(source[9:]))
            elif source.startswith(("http://", "https://")):
                chain.append(Video.fromURL(source))
            else:
                chain.append(Video.fromFileSystem(source))
        elif typ == "file":
            chain.append(File(name=str(data.get("name") or data.get("file_name") or "file"), file=str(data.get("file") or data.get("path") or ""), url=str(data.get("url") or "")))
        elif typ == "at":
            chain.append(At(name=str(data.get("name") or data.get("qq") or ""), qq=str(data.get("qq") or data.get("user_id") or "")))
        elif typ == "reply":
            chain.append(Reply(id=str(data.get("id") or data.get("message_id") or "")))
        elif typ == "json":
            chain.append(Json(data.get("data", data)))
        elif typ == "forward":
            chain.append(Forward(id=str(data.get("id") or data.get("res_id") or "")))
        else:
            chain.append(Plain(str(data.get("text") or raw)))
    return chain

