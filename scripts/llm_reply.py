#!/usr/bin/env python3
"""Helpers for OpenClaw llm-task reply generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VALID_STAGES = {"first_contact", "after_intro", "follow_up"}

RESPONSE_CONTRACT = {
    "type": "object",
    "required": ["reply_text", "conversation_stage"],
    "properties": {
        "reply_text": {
            "type": "string",
            "description": "最终可直接发送给候选人的回复文本。",
        },
        "conversation_stage": {
            "type": "string",
            "enum": sorted(VALID_STAGES),
        },
    },
    "additionalProperties": False,
}


def load_json_arg(raw_json: str | None, json_file: str | None, *, missing_message: str) -> dict[str, Any]:
    if raw_json:
        payload = json.loads(raw_json)
    elif json_file:
        with Path(json_file).open("r", encoding="utf-8-sig") as fh:
            payload = json.load(fh)
    else:
        raise ValueError(missing_message)

    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object.")
    return payload


def normalize_reply_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def build_candidate_payload(candidate: dict[str, Any], default_company_name: str) -> dict[str, Any]:
    payload = dict(candidate)
    payload["company_name"] = str(payload.get("company_name") or default_company_name)

    messages = payload.get("recent_messages")
    if not isinstance(messages, list):
        payload["recent_messages"] = []

    return payload


def build_llm_reply_prompt() -> str:
    return "\n".join(
        [
            "你是 Boss 直聘招聘场景里的中文 HR 初筛助手。",
            "你的任务是基于输入 JSON，为候选人生成一条可直接发送的中文回复。",
            "只输出符合 schema 的 JSON，不要输出 Markdown，不要输出解释。",
            "回复要求：礼貌、简洁、口语化，一条消息只推进下一步，不要重复候选人已提供的信息。",
            "优先围绕 must_ask 提问，但最多 3 到 4 个高价值问题。",
            "不要承诺薪资、offer、面试通过、入职日期。",
            "conversation_stage 只能返回 first_contact、after_intro、follow_up 之一。",
        ]
    )


def build_llm_task(config: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    llm_reply = config["llm_reply"]
    reply_config = config["reply"]
    candidate_payload = build_candidate_payload(candidate, reply_config["default_company_name"])

    return {
        "tool": str(llm_reply.get("tool_name") or "llm-task"),
        "args": {
            "prompt": build_llm_reply_prompt(),
            "input": candidate_payload,
            "schema": RESPONSE_CONTRACT,
            "thinking": str(llm_reply.get("thinking") or "low"),
            "temperature": float(llm_reply.get("temperature") or 0.2),
            "maxTokens": int(llm_reply.get("max_tokens") or 800),
            "timeoutMs": int(llm_reply.get("timeout_ms") or 30000),
        },
    }


def unwrap_llm_result(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("structured_output", "output", "result", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return unwrap_llm_result(nested)
    return payload


def normalize_llm_result(payload: dict[str, Any], stage_hint: str) -> dict[str, Any]:
    result = unwrap_llm_result(payload)

    reply_value = result.get("reply_text")
    if not isinstance(reply_value, str) or not reply_value.strip():
        raise ValueError("llm-task result must include non-empty reply_text.")
    reply_text = normalize_reply_text(reply_value)
    if not reply_text:
        raise ValueError("llm-task result reply_text cannot be blank after normalization.")

    stage_value = result.get("conversation_stage")
    if not isinstance(stage_value, str) or stage_value.strip() not in VALID_STAGES:
        raise ValueError("llm-task result must include valid conversation_stage.")
    conversation_stage = stage_value.strip()

    return {
        "reply_text": reply_text,
        "conversation_stage": conversation_stage,
        "raw": result,
    }
