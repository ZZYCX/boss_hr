#!/usr/bin/env python3
"""Unified entry point for boss-hr-assistant."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from parse_boss_snapshot import load_snapshot_text, parse_snapshot
from render_reply import build_reply, choose_job_family, detect_manual_review, infer_stage
from rename_resume import normalize_delivery_time, pick_destination, sanitize_segment
from resolve_download import parse_known_files, resolve_download
from validate_config import load_toml, validate_config


STATE_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(
    command: str,
    action: str,
    data: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    ok: bool = True,
) -> int:
    payload = {
        "ok": ok,
        "command": command,
        "action": action,
        "data": data or {},
        "errors": errors or [],
        "timestamp": utc_now(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def read_text_arg(raw_text: str | None, text_file: str | None) -> str:
    if raw_text:
        return raw_text
    if text_file:
        return Path(text_file).read_text(encoding="utf-8-sig")
    return ""


def load_candidate(candidate_json: str | None, candidate_file: str | None) -> dict[str, Any]:
    if candidate_json:
        return json.loads(candidate_json)
    if candidate_file:
        with Path(candidate_file).open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    raise ValueError("Provide --candidate-json or --candidate-file.")


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "session": {}, "threads": {}}


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: Path, state: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def path_from_config(config: dict[str, Any], section: str, key: str) -> Path:
    return Path(config[section][key])


def normalize_text(value: str) -> str:
    return value.casefold()


def derive_thread_key(candidate: dict[str, Any], thread_id: str | None) -> str:
    if thread_id:
        return thread_id
    parts = [
        str(candidate.get("candidate_name", "")),
        str(candidate.get("job_title", "")),
        str(candidate.get("delivery_time", "")),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"thread_{digest}"


def message_fingerprint(candidate: dict[str, Any], stage: str) -> str:
    messages = candidate.get("recent_messages", [])
    serial = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{stage}|{serial}".encode("utf-8")).hexdigest()


def build_resume_key(job_title: str, candidate_name: str, delivery_time: str, ext: str) -> str:
    raw = "|".join((job_title, candidate_name, delivery_time, ext.lower()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def status_list(record: dict[str, Any]) -> list[str]:
    current = record.get("statuses")
    if isinstance(current, list):
        return [str(item) for item in current]
    return []


def upsert_status(record: dict[str, Any], status: str) -> None:
    current = status_list(record)
    if status not in current:
        current.append(status)
    record["statuses"] = current


def merge_candidate(snapshot_candidate: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    candidate = dict(snapshot_candidate)
    if not override:
        return candidate

    for key, value in override.items():
        if key == "recent_messages":
            if isinstance(value, list) and value:
                candidate[key] = value
            continue
        if value not in ("", None, [], {}):
            candidate[key] = value
    return candidate


def command_validate_config(config: dict[str, Any]) -> int:
    errors = validate_config(config)
    if errors:
        return emit("validate-config", "invalid_config", {}, errors, ok=False)
    data = {
        "job_family_count": len(config["job_families"]),
        "archive_root": config["storage"]["archive_root"],
        "state_file": config["state"]["state_file"],
    }
    return emit("validate-config", "config_valid", data)


def session_action(config: dict[str, Any], current_url: str, page_text: str) -> tuple[str, dict[str, Any]]:
    session_config = config["session"]
    text = normalize_text(page_text)
    parsed = urlparse(current_url)
    host = parsed.netloc.casefold()
    url = current_url.casefold()

    login_hits = [kw for kw in session_config["login_required_keywords"] if normalize_text(kw) in text]
    blocked_hits = [kw for kw in session_config["blocked_keywords"] if normalize_text(kw) in text]
    ready_hits = [kw for kw in session_config["chat_ready_keywords"] if normalize_text(kw) in text]
    host_ok = any(host == normalize_text(item) for item in session_config["allowed_hosts"])
    path_ok = any(item.casefold() in url for item in session_config["chat_url_keywords"])

    data = {
        "current_url": current_url,
        "host_ok": host_ok,
        "path_ok": path_ok,
        "login_hits": login_hits,
        "blocked_hits": blocked_hits,
        "ready_hits": ready_hits,
    }

    if blocked_hits:
        return "risk_control", data
    if login_hits or not host_ok:
        return "login_required", data
    if not path_ok:
        return "wrong_page", data
    return "session_ok", data


def command_session_check(config: dict[str, Any], current_url: str, page_text: str, write_state: bool) -> int:
    action, data = session_action(config, current_url, page_text)
    if write_state:
        state_path = path_from_config(config, "state", "state_file")
        state = load_state(state_path)
        state["session"] = {"last_action": action, "checked_at": utc_now(), **data}
        save_state(state_path, state)
    return emit("session-check", action, data)


def command_parse_snapshot(config: dict[str, Any], current_url: str, snapshot_text: str) -> int:
    parsed = parse_snapshot(config, current_url, snapshot_text)
    return emit("parse-snapshot", "snapshot_parsed", parsed)


def evaluate_draft_reply(
    config: dict[str, Any],
    candidate: dict[str, Any],
    thread_id: str | None,
    write_state: bool,
    force: bool,
) -> tuple[str, dict[str, Any]]:
    state_path = path_from_config(config, "state", "state_file")
    state = load_state(state_path) if write_state else default_state()
    thread_key = derive_thread_key(candidate, thread_id)
    thread_record = state["threads"].get(thread_key, {})

    reply_config = config["reply"]
    stage = infer_stage(candidate, reply_config)
    fingerprint = message_fingerprint(candidate, stage)
    previous_fingerprint = thread_record.get("last_message_fingerprint")
    previous_action = thread_record.get("last_reply_action")

    if not force and previous_fingerprint == fingerprint and previous_action in {"reply", "manual_review"}:
        return (
            "skip_duplicate",
            {
                "thread_id": thread_key,
                "candidate_name": candidate.get("candidate_name", ""),
                "job_title": candidate.get("job_title", ""),
                "previous_action": previous_action,
            },
        )

    reason = detect_manual_review(candidate, reply_config)
    if reason:
        data = {
            "thread_id": thread_key,
            "candidate_name": candidate.get("candidate_name", ""),
            "job_title": candidate.get("job_title", ""),
            "stage": stage,
            "reason": reason,
        }
        if write_state:
            record = {
                **thread_record,
                "candidate_name": candidate.get("candidate_name", ""),
                "job_title": candidate.get("job_title", ""),
                "delivery_time": candidate.get("delivery_time", ""),
                "last_message_fingerprint": fingerprint,
                "last_reply_action": "manual_review",
                "last_reply_at": utc_now(),
                "updated_at": utc_now(),
            }
            upsert_status(record, "manual_review")
            state["threads"][thread_key] = record
            save_state(state_path, state)
            append_jsonl(
                path_from_config(config, "state", "reply_log_file"),
                {
                    "thread_id": thread_key,
                    "candidate_name": candidate.get("candidate_name", ""),
                    "job_title": candidate.get("job_title", ""),
                    "action": "manual_review",
                    "reason": reason,
                    "timestamp": utc_now(),
                },
            )
        return "manual_review", data

    family = choose_job_family(str(candidate["job_title"]), config["job_families"])
    reply_text = build_reply(candidate, family, reply_config, stage)
    data = {
        "thread_id": thread_key,
        "stage": stage,
        "job_family": family["name"],
        "candidate_name": candidate["candidate_name"],
        "job_title": candidate["job_title"],
        "reply": reply_text,
    }

    if write_state:
        record = {
            **thread_record,
            "candidate_name": candidate.get("candidate_name", ""),
            "job_title": candidate.get("job_title", ""),
            "delivery_time": candidate.get("delivery_time", ""),
            "last_message_fingerprint": fingerprint,
            "last_reply_action": "reply",
            "last_reply_text": reply_text,
            "last_reply_at": utc_now(),
            "updated_at": utc_now(),
        }
        upsert_status(record, "drafted")
        state["threads"][thread_key] = record
        save_state(state_path, state)
        append_jsonl(
            path_from_config(config, "state", "reply_log_file"),
            {
                "thread_id": thread_key,
                "candidate_name": candidate.get("candidate_name", ""),
                "job_title": candidate.get("job_title", ""),
                "action": "reply",
                "stage": stage,
                "timestamp": utc_now(),
            },
        )

    return "reply", data


def command_draft_reply(
    config: dict[str, Any],
    candidate: dict[str, Any],
    thread_id: str | None,
    write_state: bool,
    force: bool,
) -> int:
    action, data = evaluate_draft_reply(config, candidate, thread_id, write_state, force)
    return emit("draft-reply", action, data)


def command_mark_thread(
    config: dict[str, Any],
    thread_id: str,
    status: str,
    candidate_name: str | None,
    job_title: str | None,
    note: str | None,
) -> int:
    state_path = path_from_config(config, "state", "state_file")
    state = load_state(state_path)
    record = state["threads"].get(thread_id, {})

    if candidate_name:
        record["candidate_name"] = candidate_name
    if job_title:
        record["job_title"] = job_title
    if note:
        record["note"] = note
    record["updated_at"] = utc_now()
    upsert_status(record, status)

    state["threads"][thread_id] = record
    save_state(state_path, state)
    return emit("mark-thread", "state_updated", {"thread_id": thread_id, "status": status})


def command_show_state(config: dict[str, Any], thread_id: str | None) -> int:
    state_path = path_from_config(config, "state", "state_file")
    state = load_state(state_path)
    if thread_id:
        data = {"thread_id": thread_id, "thread": state.get("threads", {}).get(thread_id)}
    else:
        data = state
    return emit("show-state", "state_loaded", data)


def plan_resume_destination(
    config: dict[str, Any],
    source: Path,
    job_title: str,
    candidate_name: str,
    delivery_time: str,
) -> tuple[Path, str]:
    storage = config["storage"]
    payload = {
        "job_title": sanitize_segment(job_title),
        "candidate_name": sanitize_segment(candidate_name),
        "delivery_time": normalize_delivery_time(delivery_time, storage["delivery_time_format"]),
    }
    filename = storage["resume_name_pattern"].format(**payload) + source.suffix.lower()
    return pick_destination(Path(storage["archive_root"]) / filename), payload["delivery_time"]


def command_rename_resume(
    config: dict[str, Any],
    source: str,
    job_title: str,
    candidate_name: str,
    delivery_time: str,
    thread_id: str | None,
    write_state: bool,
    force: bool,
    copy: bool,
    dry_run: bool,
) -> int:
    source_path = Path(source)
    if not source_path.exists():
        return emit("rename-resume", "source_missing", {}, [f"Source file not found: {source_path}"], ok=False)

    target_path, normalized_time = plan_resume_destination(config, source_path, job_title, candidate_name, delivery_time)
    key = build_resume_key(job_title, candidate_name, normalized_time, source_path.suffix)
    fallback_id = hashlib.sha256(f"{job_title}|{candidate_name}|{normalized_time}".encode("utf-8")).hexdigest()[:16]
    thread_key = thread_id or f"resume_{fallback_id}"

    if write_state:
        state_path = path_from_config(config, "state", "state_file")
        state = load_state(state_path)
        record = state["threads"].get(thread_key, {})
        existing_keys = record.get("resume_keys", [])
        if not force and key in existing_keys:
            return emit(
                "rename-resume",
                "skip_duplicate",
                {
                    "thread_id": thread_key,
                    "candidate_name": candidate_name,
                    "job_title": job_title,
                    "delivery_time": normalized_time,
                },
            )
    else:
        state_path = path_from_config(config, "state", "state_file")
        state = default_state()

    if not dry_run:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if copy:
            from shutil import copy2

            copy2(source_path, target_path)
        else:
            from shutil import move

            move(str(source_path), target_path)

    data = {
        "thread_id": thread_key,
        "candidate_name": candidate_name,
        "job_title": job_title,
        "delivery_time": normalized_time,
        "target_path": str(target_path),
        "copied": copy,
        "dry_run": dry_run,
    }

    if write_state:
        record = state["threads"].get(thread_key, {})
        record["candidate_name"] = candidate_name
        record["job_title"] = job_title
        record["delivery_time"] = delivery_time
        record["updated_at"] = utc_now()
        resume_keys = record.get("resume_keys", [])
        if key not in resume_keys:
            resume_keys.append(key)
        record["resume_keys"] = resume_keys
        resume_files = record.get("resume_files", [])
        if str(target_path) not in resume_files:
            resume_files.append(str(target_path))
        record["resume_files"] = resume_files
        upsert_status(record, "resume_downloaded")
        state["threads"][thread_key] = record
        save_state(state_path, state)
        append_jsonl(
            path_from_config(config, "state", "resume_log_file"),
            {
                "thread_id": thread_key,
                "candidate_name": candidate_name,
                "job_title": job_title,
                "action": "renamed" if not dry_run else "dry_run",
                "target_path": str(target_path),
                "timestamp": utc_now(),
            },
        )

    return emit("rename-resume", "renamed" if not dry_run else "dry_run", data)


def choose_browser_target(target_block: dict[str, Any]) -> dict[str, Any]:
    refs = target_block.get("refs", [])
    selectors = target_block.get("selectors", [])
    primary_ref = refs[0] if refs else {}
    primary_selector = selectors[0] if selectors else ""
    return {
        "ref": primary_ref.get("ref", ""),
        "label": primary_ref.get("label", ""),
        "selector": primary_selector,
        "refs": refs,
        "selectors": selectors,
    }


def normalize_name(value: str) -> str:
    return "".join(value.casefold().split())


def candidate_name_matches(expected_name: str, observed_name: str) -> bool:
    if not expected_name or not observed_name:
        return False
    expected = normalize_name(expected_name)
    observed = normalize_name(observed_name)
    return expected == observed or expected in observed or observed in expected


def build_thread_verification(config: dict[str, Any], candidate_name: str) -> dict[str, Any]:
    return {
        "requires_resnapshot": True,
        "command": "verify-thread-open",
        "candidate_name": candidate_name,
        "expected_page_kind": "thread_view",
        "success_action": "thread_verified",
        "retry_action": "try_next_fallback",
        "required_keywords": config["browser_actions"]["thread_open_verification_texts"],
    }


def build_open_thread_action(
    config: dict[str, Any],
    thread_target: dict[str, Any],
    candidate_name: str,
) -> dict[str, Any]:
    selectors = thread_target.get("selector_candidates", [])
    js_click_fallback = thread_target.get("js_click_fallback", "")
    verification = build_thread_verification(config, candidate_name)
    return {
        "executor": "browser",
        "kind": "click",
        "description": "打开候选人会话",
        "target_type": "thread_item",
        "target_preference": "container_selector_first",
        "candidate_name": candidate_name,
        "ref": "",
        "label": thread_target.get("label", ""),
        "selector": selectors[0] if selectors else "",
        "selectors": selectors,
        "fallback_refs": (
            [{"ref": thread_target.get("ref", ""), "label": thread_target.get("label", "")}]
            if thread_target.get("ref")
            else []
        ),
        "fallback_clicks": [
            {
                "method": "selector",
                "description": "优先点击包含候选人姓名的对话项外层可点击容器。",
                "selectors": selectors,
            },
            {
                "method": "ref",
                "description": "若容器 selector 失败，再尝试 snapshot 提供的线程 ref。",
                "ref": thread_target.get("ref", ""),
            },
            {
                "method": "javascript",
                "description": "若 selector 仍失败，执行 JavaScript 向上查找可点击父节点后 click。",
                "script": js_click_fallback,
            },
        ],
        "verification": verification,
    }


def build_input_verification_script(expected_text: str) -> str:
    lines = [line.strip() for line in expected_text.splitlines() if line.strip()]
    return "\n".join(
        [
            "(() => {",
            f"  const selector = {json.dumps('#boss-chat-editor-input', ensure_ascii=False)};",
            f"  const expectedLines = {json.dumps(lines, ensure_ascii=False)};",
            "  const el = document.querySelector(selector);",
            "  if (!el) return { ok: false, reason: 'input-not-found' };",
            '  const text = ((el.innerText || el.value || el.textContent || "")).replace(/\\r/g, "").trim();',
            "  const ok = expectedLines.every((line) => text.includes(line));",
            "  return { ok, observedText: text, expectedLines };",
            "})()",
        ]
    )


def build_send_verification_script(expected_text: str) -> str:
    normalized_expected = " ".join(expected_text.split())
    return "\n".join(
        [
            "(() => {",
            f"  const expected = {json.dumps(normalized_expected, ensure_ascii=False)};",
            '  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();',
            "  const nodes = Array.from(document.querySelectorAll('body *'));",
            "  for (const node of nodes) {",
            "    if (node.matches('#boss-chat-editor-input, #boss-chat-editor-input *')) continue;",
            "    const text = normalize(node.innerText || node.textContent || '');",
            "    if (text && text.includes(expected)) {",
            "      return { ok: true, observedText: text, tagName: node.tagName };",
            "    }",
            "  }",
            "  return { ok: false, reason: 'reply-text-not-found-in-thread' };",
            "})()",
        ]
    )


def build_reply_visibility_verification(config: dict[str, Any], candidate_name: str, reply_text: str) -> dict[str, Any]:
    return {
        "requires_resnapshot": True,
        "command": "verify-reply-sent",
        "candidate_name": candidate_name,
        "expected_reply": reply_text,
        "required_keywords": config["browser_actions"]["thread_open_verification_texts"],
        "success_action": "reply_sent",
        "retry_action": "send_unverified",
    }


def build_plan_actions(
    config: dict[str, Any],
    parsed: dict[str, Any],
    reply_data: dict[str, Any],
    allow_send: bool,
    include_resume: bool,
) -> tuple[str, dict[str, Any]]:
    candidate = parsed["candidate"]
    thread_id = reply_data["thread_id"]
    browser_actions: list[dict[str, Any]] = []
    post_actions: list[dict[str, Any]] = []

    input_target = choose_browser_target(parsed["reply_input"])
    send_target = choose_browser_target(parsed["send_button"])
    input_fallback_refs = input_target["refs"]
    send_fallback_refs = send_target["refs"]

    if input_target["ref"] or input_target["selector"]:
        browser_actions.append(
            {
                "executor": "browser",
                "kind": "type",
                "description": "输入回复内容",
                "target_type": "chat_input",
                "target_preference": "selector_first",
                "ref": "",
                "fallback_refs": input_fallback_refs,
                "selector": "#boss-chat-editor-input",
                "selectors": input_target["selectors"],
                "text": reply_data["reply"],
                "html": "<br>".join([line for line in reply_data["reply"].splitlines() if line.strip()]),
                "dispatch_events": ["input", "change", "blur"],
                "verification": {
                    "method": "javascript",
                    "script": build_input_verification_script(reply_data["reply"]),
                    "success_action": "input_verified",
                    "retry_action": "retry_input",
                },
            }
        )

    if allow_send:
        browser_actions.append(
            {
                "executor": "browser",
                "kind": "click",
                "description": "点击发送按钮",
                "target_type": "send_button",
                "target_preference": "selector_first",
                "ref": "",
                "fallback_refs": send_fallback_refs,
                "selector": "div.submit.active",
                "selectors": send_target["selectors"],
                "label": send_target["label"],
                "verification": {
                    "method": "javascript",
                    "script": build_send_verification_script(reply_data["reply"]),
                    **build_reply_visibility_verification(config, candidate.get("candidate_name", ""), reply_data["reply"]),
                },
            }
        )
        post_actions.append(
            {
                "executor": "skill",
                "kind": "mark-thread",
                "args": {
                    "thread_id": thread_id,
                    "status": "replied",
                    "candidate_name": candidate.get("candidate_name", ""),
                    "job_title": candidate.get("job_title", ""),
                },
            }
        )

    if include_resume and parsed["resume_targets"]:
        resume_target = parsed["resume_targets"][0]
        selectors = resume_target.get("selectors", [])
        browser_actions.append(
            {
                "executor": "browser",
                "kind": "click",
                "description": "触发简历下载",
                "ref": resume_target.get("ref", ""),
                "selector": selectors[0] if selectors else "",
                "selectors": selectors,
                "label": resume_target.get("label", ""),
            }
        )

    action = "reply_and_download_ready" if include_resume and parsed["resume_targets"] else "reply_ready"
    if not allow_send:
        action = "draft_ready"

    return action, {
        "thread_id": thread_id,
        "candidate": candidate,
        "reply": reply_data["reply"],
        "stage": reply_data["stage"],
        "job_family": reply_data["job_family"],
        "browser_actions": browser_actions,
        "post_actions": post_actions,
        "download_expected": bool(parsed["resume_targets"]),
        "resume_targets": parsed["resume_targets"],
    }


def command_verify_thread_open(
    config: dict[str, Any],
    current_url: str,
    snapshot_text: str,
    candidate_name: str,
) -> int:
    parsed = parse_snapshot(config, current_url, snapshot_text)
    visible_text = parsed["visible_text"]
    observed_name = str(parsed["candidate"].get("candidate_name", ""))
    keyword_hits = [
        keyword
        for keyword in config["browser_actions"]["thread_open_verification_texts"]
        if keyword in visible_text
    ]
    name_match = candidate_name_matches(candidate_name, observed_name) or (
        candidate_name and normalize_text(candidate_name) in normalize_text(visible_text)
    )
    has_input_ref = bool(parsed["reply_input"].get("refs"))
    has_send_ref = bool(parsed["send_button"].get("refs"))

    data = {
        "expected_candidate_name": candidate_name,
        "observed_candidate_name": observed_name,
        "page_kind": parsed["page_kind"],
        "name_match": name_match,
        "keyword_hits": keyword_hits,
        "has_input_ref": has_input_ref,
        "has_send_ref": has_send_ref,
        "parsed": parsed,
    }

    if parsed["page_kind"] == "thread_view" and name_match and (keyword_hits or has_input_ref or has_send_ref):
        return emit("verify-thread-open", "thread_verified", data)
    if parsed["page_kind"] == "chat_list":
        return emit("verify-thread-open", "still_in_list", data)
    if parsed["page_kind"] == "thread_view":
        return emit("verify-thread-open", "thread_mismatch", data)
    return emit("verify-thread-open", "thread_open_uncertain", data)


def command_verify_reply_sent(
    config: dict[str, Any],
    current_url: str,
    snapshot_text: str,
    candidate_name: str,
    expected_reply: str,
) -> int:
    parsed = parse_snapshot(config, current_url, snapshot_text)
    visible_text = parsed["visible_text"]
    observed_name = str(parsed["candidate"].get("candidate_name", ""))
    name_match = candidate_name_matches(candidate_name, observed_name) or (
        candidate_name and normalize_text(candidate_name) in normalize_text(visible_text)
    )
    expected_lines = [line.strip() for line in expected_reply.splitlines() if line.strip()]
    visible_match = all(line in visible_text for line in expected_lines) if expected_lines else False
    keyword_hits = [
        keyword
        for keyword in config["browser_actions"]["thread_open_verification_texts"]
        if keyword in visible_text
    ]

    data = {
        "expected_candidate_name": candidate_name,
        "observed_candidate_name": observed_name,
        "page_kind": parsed["page_kind"],
        "name_match": name_match,
        "visible_match": visible_match,
        "expected_reply": expected_reply,
        "expected_lines": expected_lines,
        "keyword_hits": keyword_hits,
        "parsed": parsed,
    }

    if parsed["page_kind"] == "thread_view" and name_match and visible_match:
        return emit("verify-reply-sent", "reply_sent", data)
    if parsed["page_kind"] == "thread_view" and name_match:
        return emit("verify-reply-sent", "reply_send_unverified", data)
    if parsed["page_kind"] == "thread_view":
        return emit("verify-reply-sent", "thread_mismatch", data)
    return emit("verify-reply-sent", "reply_send_uncertain", data)


def command_plan_next_action(
    config: dict[str, Any],
    current_url: str,
    snapshot_text: str,
    thread_id: str | None,
    candidate_override: dict[str, Any] | None,
    allow_send: bool,
    include_resume: bool,
    force: bool,
) -> int:
    parsed = parse_snapshot(config, current_url, snapshot_text)
    session_result, session_data = session_action(config, current_url, parsed["visible_text"])
    if session_result != "session_ok":
        return emit("plan-next-action", session_result, {"session": session_data, "parsed": parsed})

    page_kind = parsed["page_kind"]
    if page_kind == "chat_empty":
        return emit("plan-next-action", "wait_for_candidates", {"parsed": parsed})

    if page_kind == "chat_list":
        thread_refs = parsed["thread_refs"]
        if not thread_refs:
            return emit("plan-next-action", "wait_for_candidates", {"parsed": parsed})
        target_candidate_name = ""
        if candidate_override:
            target_candidate_name = str(candidate_override.get("candidate_name", "")).strip()

        selected_thread = thread_refs[0]
        if target_candidate_name:
            matched = next(
                (
                    thread_ref
                    for thread_ref in thread_refs
                    if candidate_name_matches(target_candidate_name, str(thread_ref.get("label", "")))
                ),
                None,
            )
            if not matched:
                return emit(
                    "plan-next-action",
                    "thread_not_found",
                    {
                        "target_candidate_name": target_candidate_name,
                        "available_threads": [item.get("label", "") for item in thread_refs],
                        "parsed": parsed,
                    },
                )
            selected_thread = matched

        open_thread_action = build_open_thread_action(
            config,
            selected_thread,
            target_candidate_name or str(selected_thread.get("label", "")),
        )
        return emit(
            "plan-next-action",
            "open_thread",
            {
                "parsed": parsed,
                "target_candidate_name": open_thread_action["candidate_name"],
                "browser_actions": [open_thread_action],
                "verification": open_thread_action["verification"],
            },
        )

    if page_kind != "thread_view":
        return emit("plan-next-action", "unknown_page", {"parsed": parsed})

    candidate = merge_candidate(parsed["candidate"], candidate_override)
    resolved_thread_id = derive_thread_key(candidate, thread_id)
    candidate["thread_id"] = resolved_thread_id
    parsed["candidate"] = candidate

    if not candidate.get("candidate_name") or not candidate.get("job_title"):
        return emit("plan-next-action", "candidate_incomplete", {"parsed": parsed, "candidate": candidate})
    if not candidate.get("recent_messages"):
        return emit("plan-next-action", "candidate_incomplete", {"parsed": parsed, "candidate": candidate})

    draft_action, draft_data = evaluate_draft_reply(config, candidate, resolved_thread_id, write_state=False, force=force)
    if draft_action in {"manual_review", "skip_duplicate"}:
        return emit("plan-next-action", draft_action, {"parsed": parsed, "candidate": candidate, **draft_data})

    action, data = build_plan_actions(config, parsed, draft_data, allow_send, include_resume)
    data["parsed"] = parsed
    return emit("plan-next-action", action, data)


def command_resolve_download(
    config: dict[str, Any],
    download_root: str | None,
    known_files_json: str | None,
    known_files_file: str | None,
    after_epoch: float | None,
) -> int:
    try:
        known = parse_known_files(known_files_json, known_files_file)
    except (ValueError, json.JSONDecodeError) as exc:
        return emit("resolve-download", "known_files_invalid", {}, [str(exc)], ok=False)

    data = resolve_download(config, download_root, known, after_epoch)
    return emit("resolve-download", data["action"], data)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unified entry point for boss-hr-assistant.")
    p.add_argument("--config", required=True, help="Path to TOML config.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-config", help="Validate config and emit JSON.")

    session = sub.add_parser("session-check", help="Check whether the Boss session is usable.")
    session.add_argument("--current-url", default="", help="Current browser URL.")
    session.add_argument("--page-text", help="Visible page text.")
    session.add_argument("--page-file", help="UTF-8 text snapshot file.")
    session.add_argument("--write-state", action="store_true", help="Persist session result.")

    parse_snapshot_cmd = sub.add_parser("parse-snapshot", help="Parse Browser Relay snapshot text.")
    parse_snapshot_cmd.add_argument("--current-url", default="", help="Current browser URL.")
    parse_snapshot_cmd.add_argument("--snapshot-text", help="Raw snapshot text.")
    parse_snapshot_cmd.add_argument("--snapshot-file", help="Snapshot text file.")

    verify_thread_cmd = sub.add_parser("verify-thread-open", help="Verify that the clicked thread is the expected candidate.")
    verify_thread_cmd.add_argument("--current-url", default="", help="Current browser URL.")
    verify_thread_cmd.add_argument("--snapshot-text", help="Raw snapshot text.")
    verify_thread_cmd.add_argument("--snapshot-file", help="Snapshot text file.")
    verify_thread_cmd.add_argument("--candidate-name", required=True, help="Expected candidate name after opening the thread.")

    verify_send_cmd = sub.add_parser("verify-reply-sent", help="Verify that the reply was actually sent in the expected thread.")
    verify_send_cmd.add_argument("--current-url", default="", help="Current browser URL.")
    verify_send_cmd.add_argument("--snapshot-text", help="Raw snapshot text.")
    verify_send_cmd.add_argument("--snapshot-file", help="Snapshot text file.")
    verify_send_cmd.add_argument("--candidate-name", required=True, help="Expected candidate name after sending.")
    verify_send_cmd.add_argument("--expected-reply", required=True, help="Expected reply text that should be visible after sending.")

    plan = sub.add_parser("plan-next-action", help="Plan the next Browser Relay action from a snapshot.")
    plan.add_argument("--current-url", default="", help="Current browser URL.")
    plan.add_argument("--snapshot-text", help="Raw snapshot text.")
    plan.add_argument("--snapshot-file", help="Snapshot text file.")
    plan.add_argument("--thread-id", help="Stable Boss thread ID if available.")
    plan.add_argument("--candidate-json", help="Optional candidate JSON override.")
    plan.add_argument("--candidate-file", help="Optional candidate JSON override file.")
    plan.add_argument("--allow-send", action="store_true", help="Include send-button browser actions.")
    plan.add_argument("--include-resume", action="store_true", help="Include resume download actions when available.")
    plan.add_argument("--force", action="store_true", help="Ignore duplicate fingerprint checks.")

    draft = sub.add_parser("draft-reply", help="Generate a reply and dedupe by thread state.")
    draft.add_argument("--candidate-json", help="Candidate JSON string.")
    draft.add_argument("--candidate-file", help="Candidate JSON file.")
    draft.add_argument("--thread-id", help="Stable Boss thread ID if available.")
    draft.add_argument("--write-state", action="store_true", help="Persist reply action and fingerprint.")
    draft.add_argument("--force", action="store_true", help="Ignore duplicate fingerprint checks.")

    resolve_cmd = sub.add_parser("resolve-download", help="Resolve the newest stable download from the download directory.")
    resolve_cmd.add_argument("--download-root", help="Override download directory.")
    resolve_cmd.add_argument("--known-files-json", help="JSON list of known file paths or names.")
    resolve_cmd.add_argument("--known-files-file", help="Path to JSON list of known file paths or names.")
    resolve_cmd.add_argument("--after-epoch", type=float, help="Only consider files modified after this Unix epoch.")

    rename = sub.add_parser("rename-resume", help="Rename, archive, and optionally record resume state.")
    rename.add_argument("--source", required=True, help="Downloaded file path.")
    rename.add_argument("--job-title", required=True, help="Candidate job title.")
    rename.add_argument("--candidate-name", required=True, help="Candidate name.")
    rename.add_argument("--delivery-time", required=True, help="Delivery time string.")
    rename.add_argument("--thread-id", help="Stable Boss thread ID if available.")
    rename.add_argument("--write-state", action="store_true", help="Persist resume archive state.")
    rename.add_argument("--force", action="store_true", help="Ignore duplicate resume checks.")
    rename.add_argument("--copy", action="store_true", help="Copy instead of move.")
    rename.add_argument("--dry-run", action="store_true", help="Only calculate destination path.")

    mark = sub.add_parser("mark-thread", help="Mark thread status after a manual or browser step.")
    mark.add_argument("--thread-id", required=True, help="Stable Boss thread ID.")
    mark.add_argument("--status", required=True, help="Status to record.")
    mark.add_argument("--candidate-name", help="Candidate name.")
    mark.add_argument("--job-title", help="Job title.")
    mark.add_argument("--note", help="Optional note.")

    show = sub.add_parser("show-state", help="Read persisted state.")
    show.add_argument("--thread-id", help="Optional thread ID filter.")

    return p


def main() -> int:
    args = parser().parse_args()

    try:
        config = load_toml(Path(args.config))
    except (OSError, ValueError) as exc:
        return emit("bootstrap", "config_read_failed", {}, [str(exc)], ok=False)

    config_errors = validate_config(config)
    if config_errors:
        return emit("bootstrap", "invalid_config", {}, config_errors, ok=False)

    if args.command == "validate-config":
        return command_validate_config(config)

    if args.command == "session-check":
        try:
            page_text = read_text_arg(args.page_text, args.page_file)
        except OSError as exc:
            return emit("session-check", "page_read_failed", {}, [str(exc)], ok=False)
        return command_session_check(config, args.current_url, page_text, args.write_state)

    if args.command == "parse-snapshot":
        try:
            snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
        except (OSError, ValueError) as exc:
            return emit("parse-snapshot", "snapshot_read_failed", {}, [str(exc)], ok=False)
        return command_parse_snapshot(config, args.current_url, snapshot_text)

    if args.command == "verify-thread-open":
        try:
            snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
        except (OSError, ValueError) as exc:
            return emit("verify-thread-open", "snapshot_read_failed", {}, [str(exc)], ok=False)
        return command_verify_thread_open(config, args.current_url, snapshot_text, args.candidate_name)

    if args.command == "verify-reply-sent":
        try:
            snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
        except (OSError, ValueError) as exc:
            return emit("verify-reply-sent", "snapshot_read_failed", {}, [str(exc)], ok=False)
        return command_verify_reply_sent(
            config,
            args.current_url,
            snapshot_text,
            args.candidate_name,
            args.expected_reply,
        )

    if args.command == "plan-next-action":
        try:
            snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
        except (OSError, ValueError) as exc:
            return emit("plan-next-action", "snapshot_read_failed", {}, [str(exc)], ok=False)

        try:
            candidate_override = (
                load_candidate(args.candidate_json, args.candidate_file)
                if args.candidate_json or args.candidate_file
                else None
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return emit("plan-next-action", "candidate_read_failed", {}, [str(exc)], ok=False)

        return command_plan_next_action(
            config,
            args.current_url,
            snapshot_text,
            args.thread_id,
            candidate_override,
            args.allow_send,
            args.include_resume,
            args.force,
        )

    if args.command == "draft-reply":
        try:
            candidate = load_candidate(args.candidate_json, args.candidate_file)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return emit("draft-reply", "candidate_read_failed", {}, [str(exc)], ok=False)
        return command_draft_reply(config, candidate, args.thread_id, args.write_state, args.force)

    if args.command == "resolve-download":
        return command_resolve_download(
            config,
            args.download_root,
            args.known_files_json,
            args.known_files_file,
            args.after_epoch,
        )

    if args.command == "rename-resume":
        return command_rename_resume(
            config,
            args.source,
            args.job_title,
            args.candidate_name,
            args.delivery_time,
            args.thread_id,
            args.write_state,
            args.force,
            args.copy,
            args.dry_run,
        )

    if args.command == "mark-thread":
        return command_mark_thread(config, args.thread_id, args.status, args.candidate_name, args.job_title, args.note)

    if args.command == "show-state":
        return command_show_state(config, args.thread_id)

    return emit("bootstrap", "unknown_command", {}, [f"Unknown command: {args.command}"], ok=False)


if __name__ == "__main__":
    sys.exit(main())
