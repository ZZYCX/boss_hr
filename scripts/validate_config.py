#!/usr/bin/env python3
"""Validate boss-hr-assistant TOML config."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


REQUIRED_PATTERN_TOKENS = {"{job_title}", "{candidate_name}", "{delivery_time}"}
REQUIRED_TEMPLATE_FIELDS = (
    "template_first_contact",
    "template_after_intro",
    "template_follow_up",
)
REQUIRED_SESSION_LIST_FIELDS = (
    "allowed_hosts",
    "chat_url_keywords",
    "login_required_keywords",
    "blocked_keywords",
    "chat_ready_keywords",
)
REQUIRED_STATE_STRING_FIELDS = (
    "state_root",
    "state_file",
    "reply_log_file",
    "resume_log_file",
)
REQUIRED_OPENCLAW_STRING_FIELDS = (
    "browser_mode",
    "preferred_browser_profile",
    "fallback_browser_profile",
    "snapshot_ref_mode",
)
REQUIRED_BROWSER_ACTIONS_LIST_FIELDS = (
    "search_box_labels",
    "conversation_filter_labels",
    "ignored_thread_labels",
    "thread_item_container_selectors",
    "thread_item_clickable_ancestor_selectors",
    "thread_open_verification_texts",
    "send_button_texts",
    "reply_input_selectors",
    "send_button_selectors",
    "resume_download_selectors",
    "no_contacts_texts",
    "no_message_texts",
)
REQUIRED_DOWNLOAD_RESOLUTION_LIST_FIELDS = (
    "extensions",
    "ignore_suffixes",
)


def load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def validate_job_family(family: dict, index: int, errors: list[str]) -> None:
    prefix = f"job_families[{index}]"

    if not is_non_empty_string(family.get("name")):
        errors.append(f"{prefix}.name is required.")
    if "title_keywords" not in family or not isinstance(family["title_keywords"], list):
        errors.append(f"{prefix}.title_keywords must be a list.")
    if not is_string_list(family.get("screening_questions")):
        errors.append(f"{prefix}.screening_questions must be a non-empty string list.")

    for key in REQUIRED_TEMPLATE_FIELDS:
        if not is_non_empty_string(family.get(key)):
            errors.append(f"{prefix}.{key} is required.")


def validate_config(config: dict) -> list[str]:
    errors: list[str] = []

    openclaw = config.get("openclaw")
    boss = config.get("boss")
    session = config.get("session")
    browser_actions = config.get("browser_actions")
    storage = config.get("storage")
    download_resolution = config.get("download_resolution")
    state = config.get("state")
    reply = config.get("reply")
    families = config.get("job_families")

    if not isinstance(openclaw, dict):
        errors.append("[openclaw] section is required.")
    if not isinstance(boss, dict):
        errors.append("[boss] section is required.")
    if not isinstance(session, dict):
        errors.append("[session] section is required.")
    if not isinstance(browser_actions, dict):
        errors.append("[browser_actions] section is required.")
    if not isinstance(storage, dict):
        errors.append("[storage] section is required.")
    if not isinstance(download_resolution, dict):
        errors.append("[download_resolution] section is required.")
    if not isinstance(state, dict):
        errors.append("[state] section is required.")
    if not isinstance(reply, dict):
        errors.append("[reply] section is required.")
    if not isinstance(families, list) or not families:
        errors.append("[[job_families]] must define at least one family.")

    if errors:
        return errors

    for key in REQUIRED_OPENCLAW_STRING_FIELDS:
        if not is_non_empty_string(openclaw.get(key)):
            errors.append(f"[openclaw].{key} is required.")
    if not isinstance(openclaw.get("snapshot_limit"), int) or openclaw["snapshot_limit"] <= 0:
        errors.append("[openclaw].snapshot_limit must be a positive integer.")
    if not isinstance(openclaw.get("snapshot_with_labels"), bool):
        errors.append("[openclaw].snapshot_with_labels must be a boolean.")
    if not isinstance(openclaw.get("require_existing_logged_in_tab"), bool):
        errors.append("[openclaw].require_existing_logged_in_tab must be a boolean.")

    for key in ("base_url", "chat_list_url"):
        if not is_non_empty_string(boss.get(key)):
            errors.append(f"[boss].{key} is required.")

    for key in ("poll_interval_seconds", "max_unread_threads_per_run", "download_timeout_seconds"):
        value = boss.get(key)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"[boss].{key} must be a positive integer.")

    for key in REQUIRED_SESSION_LIST_FIELDS:
        if not is_string_list(session.get(key)):
            errors.append(f"[session].{key} must be a non-empty string list.")

    for key in REQUIRED_BROWSER_ACTIONS_LIST_FIELDS:
        if not is_string_list(browser_actions.get(key)):
            errors.append(f"[browser_actions].{key} must be a non-empty string list.")

    for key in ("download_root", "archive_root", "message_log_root", "resume_name_pattern", "delivery_time_format"):
        if not is_non_empty_string(storage.get(key)):
            errors.append(f"[storage].{key} is required.")

    pattern = storage.get("resume_name_pattern", "")
    for token in REQUIRED_PATTERN_TOKENS:
        if token not in pattern:
            errors.append(f"[storage].resume_name_pattern must contain {token}.")

    for key in REQUIRED_DOWNLOAD_RESOLUTION_LIST_FIELDS:
        if not is_string_list(download_resolution.get(key)):
            errors.append(f"[download_resolution].{key} must be a non-empty string list.")
    for key in ("min_size_bytes", "stability_window_seconds"):
        value = download_resolution.get(key)
        if not isinstance(value, int) or value < 0:
            errors.append(f"[download_resolution].{key} must be a non-negative integer.")

    for key in REQUIRED_STATE_STRING_FIELDS:
        if not is_non_empty_string(state.get(key)):
            errors.append(f"[state].{key} is required.")
    cooldown = state.get("reply_cooldown_minutes")
    if not isinstance(cooldown, int) or cooldown < 0:
        errors.append("[state].reply_cooldown_minutes must be a non-negative integer.")

    if not is_non_empty_string(reply.get("default_company_name")):
        errors.append("[reply].default_company_name is required.")
    if not is_non_empty_string(reply.get("default_signoff")):
        errors.append("[reply].default_signoff is required.")
    if not isinstance(reply.get("max_recent_messages"), int) or reply["max_recent_messages"] <= 0:
        errors.append("[reply].max_recent_messages must be a positive integer.")

    for key in ("manual_review_keywords", "resume_trigger_keywords", "self_intro_keywords"):
        if not is_string_list(reply.get(key)):
            errors.append(f"[reply].{key} must be a non-empty string list.")

    family_names: set[str] = set()
    generic_found = False
    for index, family in enumerate(families):
        if not isinstance(family, dict):
            errors.append(f"job_families[{index}] must be a table.")
            continue
        validate_job_family(family, index, errors)
        name = family.get("name")
        if isinstance(name, str):
            if name in family_names:
                errors.append(f"Duplicate job family name: {name}.")
            family_names.add(name)
            if name == "generic":
                generic_found = True

    if not generic_found:
        errors.append("A fallback job family named 'generic' is required.")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate boss-hr-assistant config.")
    parser.add_argument("--config", required=True, help="Path to TOML config.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        return 1

    try:
        config = load_toml(config_path)
    except tomllib.TOMLDecodeError as exc:
        print(f"[ERROR] Invalid TOML: {exc}")
        return 1

    errors = validate_config(config)
    if errors:
        print("[ERROR] Config validation failed:")
        for error in errors:
            print(f" - {error}")
        return 1

    family_count = len(config["job_families"])
    print(f"[OK] Config is valid: {config_path}")
    print(f"[OK] Job families: {family_count}")
    print(f"[OK] Archive root: {config['storage']['archive_root']}")
    print(f"[OK] State file: {config['state']['state_file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
