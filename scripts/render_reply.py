#!/usr/bin/env python3
"""Render a reply draft for a Boss candidate chat."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any


VALID_STAGES = {"first_contact", "after_intro", "follow_up"}
HR_ROLES = {"hr", "recruiter", "assistant"}


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_candidate(candidate_json: str | None, candidate_file: str | None) -> dict:
    if candidate_json:
        return json.loads(candidate_json)
    if candidate_file:
        with Path(candidate_file).open("r", encoding="utf-8") as fh:
            return json.load(fh)
    raise ValueError("Provide --candidate-json or --candidate-file.")


def normalize_text(value: str) -> str:
    return value.casefold()


def recent_messages_text(messages: list[dict[str, Any]]) -> str:
    parts = []
    for message in messages:
        text = message.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def choose_job_family(job_title: str, families: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_title = normalize_text(job_title)
    generic_family = families[-1]

    for family in families:
        keywords = family.get("title_keywords", [])
        if not keywords:
            generic_family = family
            continue
        for keyword in keywords:
            if normalize_text(keyword) in normalized_title:
                return family
    return generic_family


def infer_stage(candidate: dict, reply_config: dict) -> str:
    stage = candidate.get("conversation_stage")
    if isinstance(stage, str) and stage in VALID_STAGES:
        return stage

    messages = candidate.get("recent_messages", [])
    if not isinstance(messages, list):
        return "first_contact"

    has_hr_message = any(
        isinstance(message, dict)
        and isinstance(message.get("role"), str)
        and message["role"].casefold() in HR_ROLES
        for message in messages
    )
    if has_hr_message:
        return "follow_up"

    combined_text = normalize_text(recent_messages_text(messages))
    self_intro_keywords = reply_config.get("self_intro_keywords", [])
    if any(normalize_text(keyword) in combined_text for keyword in self_intro_keywords):
        return "after_intro"

    if len(combined_text.strip()) >= 40:
        return "after_intro"

    return "first_contact"


def detect_manual_review(candidate: dict, reply_config: dict) -> str | None:
    if candidate.get("needs_manual_review") is True:
        return "candidate context flagged manual review"

    messages = candidate.get("recent_messages", [])
    if not isinstance(messages, list):
        return "recent_messages must be a list"

    combined_text = normalize_text(recent_messages_text(messages))
    for keyword in reply_config.get("manual_review_keywords", []):
        if normalize_text(keyword) in combined_text:
            return f"matched manual review keyword: {keyword}"

    if not candidate.get("job_title"):
        return "job_title is missing"
    if not candidate.get("candidate_name"):
        return "candidate_name is missing"

    return None


def build_reply(candidate: dict, family: dict, reply_config: dict, stage: str) -> str:
    template_map = {
        "first_contact": "template_first_contact",
        "after_intro": "template_after_intro",
        "follow_up": "template_follow_up",
    }
    template = family[template_map[stage]]

    messages = candidate.get("recent_messages", [])
    latest_message = ""
    if messages:
        last = messages[-1]
        if isinstance(last, dict):
            latest_message = str(last.get("text", ""))

    questions = "、".join(family.get("screening_questions", []))
    company_name = candidate.get("company_name") or reply_config["default_company_name"]
    signoff = reply_config["default_signoff"]

    payload = SafeDict(
        candidate_name=str(candidate.get("candidate_name", "")),
        job_title=str(candidate.get("job_title", "")),
        delivery_time=str(candidate.get("delivery_time", "")),
        company_name=str(company_name),
        questions=questions,
        latest_message=latest_message,
        signoff=signoff,
    )
    reply = template.format_map(payload).strip()
    return " ".join(reply.split())


def format_output(data: dict[str, Any], output_format: str) -> str:
    if output_format == "text":
        if data["action"] == "reply":
            return data["reply"]
        return data["reason"]
    return json.dumps(data, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a Boss HR reply from candidate context.")
    parser.add_argument("--config", required=True, help="Path to TOML config.")
    parser.add_argument("--candidate-json", help="Candidate context JSON string.")
    parser.add_argument("--candidate-file", help="Path to candidate context JSON file.")
    parser.add_argument("--format", choices=("text", "json"), default="json")
    args = parser.parse_args()

    try:
        config = load_toml(Path(args.config))
        candidate = load_candidate(args.candidate_json, args.candidate_file)
    except (OSError, tomllib.TOMLDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    reply_config = config.get("reply", {})
    families = config.get("job_families", [])
    if not isinstance(families, list) or not families:
        print("[ERROR] Config is missing job_families.")
        return 1

    reason = detect_manual_review(candidate, reply_config)
    if reason:
        output = {
            "action": "manual_review",
            "reason": reason,
            "candidate_name": candidate.get("candidate_name", ""),
            "job_title": candidate.get("job_title", ""),
        }
        print(format_output(output, args.format))
        return 0

    family = choose_job_family(str(candidate["job_title"]), families)
    stage = infer_stage(candidate, reply_config)
    reply = build_reply(candidate, family, reply_config, stage)

    output = {
        "action": "reply",
        "stage": stage,
        "job_family": family["name"],
        "candidate_name": candidate["candidate_name"],
        "job_title": candidate["job_title"],
        "reply": reply,
    }
    print(format_output(output, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
