"""Dependency-free redaction shared by logs, traces, and providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_REDACTED = "<redacted>"
_SENSITIVE_KEYS = {
    "api-key",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "proxy-authorization",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}


def sanitize_for_logging(value: Any) -> Any:
    """Return a JSON-serializable copy with credentials and image bytes redacted."""

    if isinstance(value, Mapping):
        block_type = value.get("type")
        if block_type == "image" and isinstance(value.get("source"), Mapping):
            source = value["source"]
            if source.get("type") == "base64" and isinstance(source.get("data"), str):
                return {
                    **{
                        str(key): sanitize_for_logging(item)
                        for key, item in value.items()
                        if key != "source"
                    },
                    "source": {
                        **{
                            str(key): sanitize_for_logging(item)
                            for key, item in source.items()
                            if key != "data"
                        },
                        "data": {
                            "redacted": True,
                            "characters": len(source["data"]),
                        },
                    },
                }
        if block_type == "image_url" and isinstance(value.get("image_url"), Mapping):
            image_url = value["image_url"]
            url = image_url.get("url")
            if isinstance(url, str) and url.startswith("data:"):
                return {
                    **{
                        str(key): sanitize_for_logging(item)
                        for key, item in value.items()
                        if key != "image_url"
                    },
                    "image_url": {
                        **{
                            str(key): sanitize_for_logging(item)
                            for key, item in image_url.items()
                            if key != "url"
                        },
                        "url": {"redacted": True, "characters": len(url)},
                    },
                }
        return {
            str(key): (
                _REDACTED
                if _is_sensitive_key(str(key))
                else sanitize_for_logging(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_for_logging(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return sanitize_for_logging(value.model_dump())
        except Exception:
            pass
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    return key.lower().replace("_", "-") in _SENSITIVE_KEYS


__all__ = ["sanitize_for_logging"]
