"""Tool-free utility completion service shared by ACP, CLI, and SDK hosts."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from box_agent.client_info import ClientInfo, scoped_client_info
from box_agent.llm.binding import bind_session_llm
from box_agent.llm.lightweight import (
    LightweightContentFiltered,
    LightweightInvalidArgs,
    LightweightPromptError,
    LightweightTimeout,
    run_lightweight_prompt,
)
from box_agent.llm.model_routing import resolve_model_client


logger = logging.getLogger(__name__)
_DEFAULT_AGENT_TITLE = "Box-Agent"
_TITLE_MAX_OUTPUT_TOKENS = 8_000


def default_utility_llm_resolver(base_llm: Any) -> Callable[[Mapping[str, Any]], Any]:
    """Build a resolver that honors the public host model-binding contract."""

    def resolve(meta: Mapping[str, Any]) -> Any:
        return bind_session_llm(base_llm, dict(meta))

    return resolve


class UtilityPromptService:
    """Run a single bounded completion without Agent Loop side effects."""

    def __init__(
        self,
        llm_resolver: Callable[[Mapping[str, Any]], Any],
        *,
        default_client_info: ClientInfo | None = None,
    ) -> None:
        self._llm_resolver = llm_resolver
        self._default_client_info = default_client_info

    async def prompt(self, params: Mapping[str, Any]) -> dict[str, Any]:
        prompt = params.get("prompt", "")
        system_prompt = params.get("systemPrompt") or None
        timeout_ms = params.get("timeoutMs")
        raw_meta = params.get("_meta")
        meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
        client_info = ClientInfo.from_meta(meta.get("client_info")) or self._default_client_info
        purpose = meta.get("purpose") or params.get("purpose") or ""
        normalized_purpose = str(purpose).strip().lower()
        if "title" in normalized_purpose:
            call_kind = "title_generate"
        elif "context_summary" in normalized_purpose:
            call_kind = "context_summary"
        else:
            call_kind = "utility"
        session_id = _meta_string(meta, "session_id")
        turn_id = _meta_string(meta, "turn_id", "turnId")
        title = (
            _meta_string(meta, "title", "session_title", "sessionTitle")
            or str(purpose).strip()
            or str(params.get("workspaceLabel") or "").strip()
            or _DEFAULT_AGENT_TITLE
        )
        workspace_label = params.get("workspaceLabel") or ""

        if not isinstance(prompt, str) or not prompt.strip():
            return {
                "error": {
                    "code": "invalid_args",
                    "message": "prompt must be a non-empty string",
                }
            }
        if system_prompt is not None and not isinstance(system_prompt, str):
            return {
                "error": {
                    "code": "invalid_args",
                    "message": "systemPrompt must be a string",
                }
            }
        if not session_id:
            session_id = f"local-agent-utility-{uuid4()}"
        if not turn_id:
            turn_id = f"{session_id}-turn-{uuid4().hex[:8]}"

        timeout = 30.0
        if timeout_ms is not None:
            try:
                timeout = max(0.001, float(timeout_ms) / 1000.0)
            except (TypeError, ValueError):
                return {
                    "error": {
                        "code": "invalid_args",
                        "message": "timeoutMs must be a number",
                    }
                }

        max_output_tokens_cap = None
        if "title" in normalized_purpose:
            routing_tags = ("summary", "rewrite", "fast")
            routing_ability = 1
            max_output_tokens_cap = _TITLE_MAX_OUTPUT_TOKENS
        elif "presentation" in normalized_purpose:
            routing_tags = ("presentation", "analysis")
            routing_ability = 1
        elif "summary" in normalized_purpose:
            routing_tags = ("summary", "fast")
            routing_ability = 1
        elif "expert" in normalized_purpose:
            routing_tags = ("analysis", "reasoning")
            routing_ability = 2
        else:
            routing_tags = None
            routing_ability = None

        try:
            utility_llm = self._llm_resolver(meta)
            utility_llm, routing_diagnostic = resolve_model_client(
                utility_llm,
                task=" ".join(
                    part
                    for part in (
                        str(purpose).strip(),
                        str(workspace_label).strip(),
                        prompt[:2_000],
                    )
                    if part
                ),
                strategy="utility",
                task_tags=routing_tags,
                required_ability_level=routing_ability,
                max_output_tokens_cap=max_output_tokens_cap,
            )
            utility_llm.set_request_context(
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
                client_info=client_info,
            )
        except (TypeError, ValueError) as exc:
            return {"error": {"code": "invalid_args", "message": str(exc)}}

        provider = getattr(utility_llm, "provider", None)
        model = getattr(utility_llm, "model", "")
        logger.info(
            "utility prompt model routing: purpose=%s workspace=%s model=%s routing=%s",
            purpose,
            workspace_label,
            model,
            routing_diagnostic,
        )
        try:
            with scoped_client_info(client_info):
                result = await run_lightweight_prompt(
                    utility_llm,
                    prompt,
                    system_prompt=system_prompt,
                    session_id=session_id,
                    turn_id=turn_id,
                    title=title,
                    call_kind=call_kind,
                    timeout=timeout,
                )
        except LightweightInvalidArgs as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}
        except LightweightContentFiltered as exc:
            logger.info("utility prompt content filtered: model=%s", model)
            return {"error": {"code": exc.code, "message": str(exc)}}
        except LightweightTimeout as exc:
            logger.warning(
                "utility prompt timeout: purpose=%s timeout_ms=%s model=%s",
                purpose,
                int(timeout * 1000),
                model,
            )
            return {"error": {"code": exc.code, "message": str(exc)}}
        except LightweightPromptError as exc:
            logger.warning(
                "utility prompt failed: purpose=%s code=%s provider=%s model=%s",
                purpose,
                exc.code,
                provider,
                model,
            )
            return {"error": {"code": exc.code, "message": str(exc)}}

        return {
            "text": result.text,
            "finishReason": result.finish_reason,
            "usage": {
                "inputTokens": result.input_tokens,
                "outputTokens": result.output_tokens,
            },
            "durationMs": result.duration_ms,
        }


def _meta_string(meta: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = ["UtilityPromptService", "default_utility_llm_resolver"]
