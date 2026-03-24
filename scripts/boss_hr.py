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

from llm_reply import build_candidate_payload, build_llm_task, load_json_arg, normalize_llm_result
from parse_boss_snapshot import build_thread_js_fallback, load_snapshot_text, parse_snapshot
from resolve_download import parse_known_files, resolve_download
from validate_config import load_toml, validate_config


STATE_VERSION = 1
PRIMARY_EDITOR_SELECTOR = "#boss-chat-editor-input"
PRIMARY_SEND_SELECTOR = "div.submit.active"


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


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "session": {}, "threads": {}}


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


def latest_message_text(candidate: dict[str, Any]) -> str:
    messages = candidate.get("recent_messages", [])
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        text = str(message.get("text", "")).strip()
        if text:
            return text
    return ""


def derive_thread_key(candidate: dict[str, Any], thread_id: str | None) -> str:
    if thread_id:
        return thread_id

    seed = json.dumps(
        {
            "candidate_name": candidate.get("candidate_name", ""),
            "job_title": candidate.get("job_title", ""),
            "delivery_time": candidate.get("delivery_time", ""),
            "latest_message": latest_message_text(candidate),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"thread_{digest}"


def status_list(record: dict[str, Any]) -> list[str]:
    current = record.get("statuses")
    if isinstance(current, list):
        return [str(item) for item in current]
    return []


def upsert_status(record: dict[str, Any], status: str) -> None:
    statuses = status_list(record)
    if status not in statuses:
        statuses.append(status)
    record["statuses"] = statuses


def choose_job_family(job_title: str, families: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_title = normalize_text(job_title)
    generic_family: dict[str, Any] = {"name": "generic", "screening_questions": []}

    for family in families:
        if not isinstance(family, dict):
            continue
        keywords = family.get("title_keywords", [])
        if not keywords:
            generic_family = family
            continue
        for keyword in keywords:
            if normalize_text(str(keyword)) in normalized_title:
                return family

    return generic_family


def merge_candidate(
    config: dict[str, Any],
    snapshot_candidate: dict[str, Any],
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate = build_candidate_payload(snapshot_candidate, config["reply"]["default_company_name"])
    if not override:
        return candidate

    for key, value in override.items():
        if value in ("", None, [], {}):
            continue
        candidate[key] = value

    return build_candidate_payload(candidate, config["reply"]["default_company_name"])


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


def command_validate_config(config: dict[str, Any]) -> int:
    errors = validate_config(config)
    if errors:
        return emit("validate-config", "invalid_config", {}, errors, ok=False)
    data = {
        "job_family_count": len(config["job_families"]),
        "download_root": config["storage"]["download_root"],
        "state_file": config["state"]["state_file"],
        "llm_tool": config["llm_reply"]["tool_name"],
    }
    return emit("validate-config", "config_valid", data)


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


def dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def build_click_action(
    description: str,
    target_block: dict[str, Any],
    *,
    target_type: str,
    primary_selector: str | None = None,
) -> dict[str, Any]:
    target = choose_browser_target(target_block)
    selectors = dedupe_strings(([primary_selector] if primary_selector else []) + list(target.get("selectors", [])))
    selector = primary_selector or target.get("selector", "")
    return {
        "executor": "browser",
        "kind": "click",
        "description": description,
        "target_type": target_type,
        "target_preference": "selector_first",
        "ref": "",
        "label": target.get("label", ""),
        "selector": selector,
        "selectors": selectors,
        "fallback_refs": target.get("refs", []),
    }


def build_wait_action(
    description: str,
    *,
    selector: str | None = None,
    selectors: list[str] | None = None,
    fn: str | None = None,
    timeout_ms: int | None = None,
    seconds: float | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "executor": "browser",
        "kind": "wait",
        "description": description,
    }
    if selector:
        action["selector"] = selector
    if selectors:
        action["selectors"] = dedupe_strings(selectors)
    if fn:
        action["fn"] = fn
    if timeout_ms is not None:
        action["timeout_ms"] = timeout_ms
    if seconds is not None:
        action["seconds"] = seconds
    if error_message:
        action["error_message"] = error_message
    return action


def build_evaluate_action(
    description: str,
    script: str,
    *,
    error_message: str | None = None,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    action = {
        "executor": "browser",
        "kind": "evaluate",
        "description": description,
        "script": script,
    }
    if error_message:
        action["error_message"] = error_message
    if timeout_ms is not None:
        action["timeout_ms"] = timeout_ms
    return action


def build_open_thread_action(config: dict[str, Any], thread_target: dict[str, Any] | None, candidate_name: str) -> list[dict[str, Any]]:
    script = (
        thread_target.get("js_click_fallback", "")
        if thread_target and thread_target.get("js_click_fallback")
        else build_thread_js_fallback(candidate_name, config)
    )
    return [
        {
            "executor": "browser",
            "kind": "evaluate",
            "description": "按候选人姓名精确打开会话",
            "target_type": "thread_item",
            "candidate_name": candidate_name,
            "script": script,
            "error_message": f"未找到候选人“{candidate_name}”的可点击会话容器。",
            "selector": (thread_target.get("selector_candidates", [""])[0] if thread_target else ""),
            "selectors": dedupe_strings(
                list(thread_target.get("selector_candidates", []) if thread_target else [])
                + list(config["browser_actions"]["thread_open_container_selectors"])
            ),
            "fallback_refs": (
                [{"ref": thread_target.get("ref", ""), "label": thread_target.get("label", "")}]
                if thread_target and thread_target.get("ref")
                else []
            ),
        },
        build_wait_action("等待会话面板刷新", seconds=1.0, timeout_ms=1000),
    ]


def build_resume_download_script(config: dict[str, Any]) -> str:
    browser_actions = config["browser_actions"]
    return "\n".join(
        [
            "(() => {",
            (
                "  const iconSelectors = "
                + json.dumps(browser_actions["resume_download_icon_selectors"], ensure_ascii=False)
                + ";"
            ),
            (
                "  const hostSelectors = "
                + json.dumps(browser_actions["resume_download_host_selectors"], ensure_ascii=False)
                + ";"
            ),
            "  const icons = [];",
            "  const seen = new Set();",
            "  for (const selector of iconSelectors) {",
            "    for (const node of document.querySelectorAll(selector)) {",
            "      if (seen.has(node)) continue;",
            "      seen.add(node);",
            "      icons.push({ selector, node });",
            "    }",
            "  }",
            "  for (const item of icons) {",
            "    for (const selector of hostSelectors) {",
            "      const host = item.node.closest(selector);",
            "      if (host) {",
            "        host.click();",
            "        return { ok: true, strategy: `host:${selector}` };",
            "      }",
            "    }",
            "    let parent = item.node.parentElement;",
            "    while (parent) {",
            "      if ([\"SPAN\", \"BUTTON\", \"A\"].includes(parent.tagName)) {",
            "        parent.click();",
            "        return { ok: true, strategy: `ancestor:${parent.tagName.toLowerCase()}` };",
            "      }",
            "      parent = parent.parentElement;",
            "    }",
            "  }",
            "  return { ok: false, reason: 'resume-download-trigger-not-found' };",
            "})()",
        ]
    )


def thread_view_matches_candidate(parsed: dict[str, Any], candidate_name: str) -> bool:
    expected = normalize_text(candidate_name).strip()
    if not expected:
        return True
    active_name = normalize_text(str(parsed.get("candidate", {}).get("candidate_name", "")).strip())
    if active_name and active_name == expected:
        return True
    detail_visible_text = normalize_text(str(parsed.get("detail_visible_text", "")).strip())
    return bool(detail_visible_text and expected in detail_visible_text)


def validate_resume_workflow(parsed: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    resume_workflow = parsed.get("resume_workflow", {})
    if not parsed.get("candidate", {}).get("has_resume"):
        return "resume_not_available", ["当前会话中未检测到可处理的附件简历。"], []

    missing_steps = [str(step) for step in resume_workflow.get("missing_required_steps", [])]
    if not missing_steps:
        return "resume_ready", [], []

    messages = {
        "resume_accept": "当前会话中缺少可点击的“同意接收附件简历”入口。",
    }
    return "resume_step_missing", [messages.get(step, f"缺少简历步骤：{step}") for step in missing_steps], missing_steps


def build_resume_download_actions(config: dict[str, Any], parsed: dict[str, Any]) -> list[dict[str, Any]]:
    resume_workflow = parsed["resume_workflow"]
    download_timeout_ms = int(config["boss"]["download_timeout_seconds"]) * 1000
    preview_modal_selectors = list(resume_workflow["preview_modal"]["selectors"])
    download_icon_selectors = list(resume_workflow["download_trigger"]["icon_selectors"])
    close_block = {"refs": [], "selectors": resume_workflow["close_button"]["selectors"]}
    return [
        build_click_action("点击同意接收附件简历", resume_workflow["accept_button"], target_type="resume_accept"),
        build_wait_action(
            "等待附件简历卡片进入可预览状态",
            selector=resume_workflow["preview_button"]["selectors"][0],
            selectors=resume_workflow["preview_button"]["selectors"],
            timeout_ms=download_timeout_ms,
            error_message="同意附件简历后，未出现可预览的简历卡片。",
        ),
        build_click_action("打开附件简历预览", resume_workflow["preview_button"], target_type="resume_preview"),
        build_wait_action(
            "等待简历预览弹层出现",
            selector=preview_modal_selectors[0],
            selectors=dedupe_strings(preview_modal_selectors + download_icon_selectors),
            timeout_ms=download_timeout_ms,
            error_message="未成功打开附件简历预览弹层。",
        ),
        build_evaluate_action(
            "触发附件简历下载",
            build_resume_download_script(config),
            timeout_ms=download_timeout_ms,
            error_message="未找到可点击的附件简历下载按钮宿主节点。",
        ),
        build_wait_action("等待浏览器接管简历下载", seconds=1.0, timeout_ms=1000),
        build_click_action("关闭简历预览弹层", close_block, target_type="resume_preview_close"),
    ]


def build_llm_reply_request(config: dict[str, Any], candidate: dict[str, Any], thread_key: str) -> dict[str, Any]:
    family = choose_job_family(str(candidate.get("job_title", "")), config["job_families"])
    llm_input = build_candidate_payload(candidate, config["reply"]["default_company_name"])
    llm_input["thread_id"] = thread_key
    llm_input["job_family"] = family.get("name", "generic")
    llm_input["must_ask"] = list(family.get("screening_questions", []))

    return {
        "thread_id": thread_key,
        "job_family": family.get("name", "generic"),
        "candidate_name": candidate.get("candidate_name", ""),
        "job_title": candidate.get("job_title", ""),
        "candidate": llm_input,
        "llm_task_request": build_llm_task(config, llm_input),
        "next_command": "finalize-reply-plan",
    }


def build_plan_actions(
    config: dict[str, Any],
    parsed: dict[str, Any],
    reply_text: str,
    thread_id: str,
    stage: str,
    job_family: str,
    allow_send: bool,
    include_resume: bool,
) -> tuple[str, dict[str, Any]]:
    candidate = parsed["candidate"]
    browser_actions: list[dict[str, Any]] = []
    post_actions: list[dict[str, Any]] = []

    input_target = choose_browser_target(parsed["reply_input"])

    browser_actions.append(
        {
            "executor": "browser",
            "kind": "type",
            "description": "输入回复内容",
            "target_type": "chat_input",
            "target_preference": "selector_first",
            "ref": "",
            "fallback_refs": input_target["refs"],
            "selector": PRIMARY_EDITOR_SELECTOR,
            "selectors": input_target["selectors"],
            "text": reply_text,
            "html": "<br>".join(line for line in reply_text.splitlines() if line.strip()),
            "dispatch_events": ["input", "change", "blur"],
        }
    )

    if allow_send:
        browser_actions.append(build_click_action("点击发送按钮", parsed["send_button"], target_type="send_button", primary_selector=PRIMARY_SEND_SELECTOR))
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

    if include_resume:
        browser_actions.extend(build_resume_download_actions(config, parsed))

    action = "reply_and_download_ready" if include_resume and parsed["candidate"].get("has_resume") and allow_send else "reply_ready"
    if not allow_send:
        action = "draft_ready"

    return action, {
        "thread_id": thread_id,
        "candidate": candidate,
        "reply": reply_text,
        "stage": stage,
        "job_family": job_family,
        "browser_actions": browser_actions,
        "post_actions": post_actions,
        "download_expected": bool(include_resume and parsed["candidate"].get("has_resume")),
        "resume_targets": parsed["resume_targets"],
        "resume_workflow": parsed.get("resume_workflow", {}),
    }


def command_plan_next_action(
    config: dict[str, Any],
    current_url: str,
    snapshot_text: str,
    thread_id: str | None,
    candidate_override: dict[str, Any] | None,
    target_candidate_name: str | None,
) -> int:
    parsed = parse_snapshot(config, current_url, snapshot_text)
    session_result, session_data = session_action(config, current_url, parsed["visible_text"])
    if session_result != "session_ok":
        return emit("plan-next-action", session_result, {"session": session_data, "parsed": parsed})

    original_requested_name = str(target_candidate_name or "").strip()
    requested_name = original_requested_name
    thread_refs = parsed["thread_refs"]
    auto_selected_thread: dict[str, Any] | None = None

    if not requested_name:
        unread_threads = [item for item in thread_refs if item.get("has_unread")]
        if not unread_threads:
            if parsed["page_kind"] == "chat_empty":
                return emit("plan-next-action", "wait_for_candidates", {"parsed": parsed})
            return emit(
                "plan-next-action",
                "wait_for_unread",
                {
                    "parsed": parsed,
                    "available_threads": [item.get("label", "") for item in thread_refs],
                },
            )
        auto_selected_thread = unread_threads[0]
        requested_name = str(auto_selected_thread.get("label", "")).strip()

    page_kind = parsed["page_kind"]
    if page_kind == "chat_empty":
        return emit("plan-next-action", "wait_for_candidates", {"parsed": parsed})

    if page_kind == "chat_list":
        if not thread_refs and not requested_name:
            return emit("plan-next-action", "wait_for_candidates", {"parsed": parsed})

        selected_thread = auto_selected_thread
        if requested_name and not selected_thread:
            selected_thread = next(
                (item for item in thread_refs if str(item.get("label", "")).strip() == requested_name),
                None,
            )

        if selected_thread is None and not requested_name:
            return emit("plan-next-action", "wait_for_candidates", {"parsed": parsed})

        candidate_name = requested_name or str(selected_thread.get("label", ""))
        open_thread_actions = build_open_thread_action(config, selected_thread, candidate_name)
        return emit(
            "plan-next-action",
            "open_thread",
            {
                "parsed": parsed,
                "target_candidate_name": candidate_name,
                "match_strategy": "snapshot_ref" if selected_thread else "dom_search",
                "available_threads": [item.get("label", "") for item in thread_refs],
                "browser_actions": open_thread_actions,
            },
        )

    if page_kind != "thread_view":
        return emit("plan-next-action", "unknown_page", {"parsed": parsed})

    if requested_name and not thread_view_matches_candidate(parsed, requested_name):
        if auto_selected_thread is not None:
            return emit(
                "plan-next-action",
                "open_thread",
                {
                    "parsed": parsed,
                    "target_candidate_name": requested_name,
                    "match_strategy": "snapshot_ref" if auto_selected_thread else "dom_search",
                    "available_threads": [item.get("label", "") for item in thread_refs],
                    "browser_actions": build_open_thread_action(config, auto_selected_thread, requested_name),
                },
            )
        return emit(
            "plan-next-action",
            "thread_not_found",
            {
                "target_candidate_name": requested_name,
                "active_candidate_name": parsed["candidate"].get("candidate_name", ""),
                "parsed": parsed,
            },
            [f"打开会话后未确认进入候选人“{requested_name}”的聊天详情。"],
            ok=False,
        )

    candidate = merge_candidate(config, parsed["candidate"], candidate_override)
    resolved_thread_id = derive_thread_key(candidate, thread_id)
    candidate["thread_id"] = resolved_thread_id
    parsed["candidate"] = candidate

    data = build_llm_reply_request(config, candidate, resolved_thread_id)
    data["parsed"] = parsed
    return emit("plan-next-action", "llm_reply_required", data)


def command_finalize_reply_plan(
    config: dict[str, Any],
    current_url: str,
    snapshot_text: str,
    candidate_override: dict[str, Any] | None,
    reply_result: dict[str, Any],
    thread_id: str | None,
    allow_send: bool,
    include_resume: bool,
) -> int:
    parsed = parse_snapshot(config, current_url, snapshot_text)
    session_result, session_data = session_action(config, current_url, parsed["visible_text"])
    if session_result != "session_ok":
        return emit("finalize-reply-plan", session_result, {"session": session_data, "parsed": parsed})

    candidate = merge_candidate(config, parsed["candidate"], candidate_override)
    resolved_thread_id = derive_thread_key(candidate, thread_id)
    candidate["thread_id"] = resolved_thread_id
    parsed["candidate"] = candidate

    stage_hint = str(reply_result.get("conversation_stage") or candidate.get("conversation_stage") or "first_contact")
    try:
        normalized = normalize_llm_result(reply_result, stage_hint)
    except ValueError as exc:
        return emit("finalize-reply-plan", "reply_result_invalid", {}, [str(exc)], ok=False)

    if include_resume:
        resume_action, resume_errors, missing_steps = validate_resume_workflow(parsed)
        if resume_errors:
            return emit(
                "finalize-reply-plan",
                resume_action,
                {"parsed": parsed, "missing_steps": missing_steps},
                resume_errors,
                ok=False,
            )

    family = choose_job_family(str(candidate.get("job_title", "")), config["job_families"])
    action, data = build_plan_actions(
        config,
        parsed,
        normalized["reply_text"],
        resolved_thread_id,
        normalized["conversation_stage"],
        family.get("name", "generic"),
        allow_send,
        include_resume,
    )

    data["parsed"] = parsed
    data["reply_result"] = {
        "reply_text": normalized["reply_text"],
        "conversation_stage": normalized["conversation_stage"],
    }
    return emit("finalize-reply-plan", action, data)


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
    record["thread_id"] = thread_id
    record["updated_at"] = utc_now()
    upsert_status(record, status)

    state["threads"][thread_id] = record
    save_state(state_path, state)
    append_jsonl(
        path_from_config(config, "state", "reply_log_file"),
        {
            "thread_id": thread_id,
            "candidate_name": record.get("candidate_name", ""),
            "job_title": record.get("job_title", ""),
            "action": status,
            "note": note or "",
            "timestamp": utc_now(),
        },
    )
    return emit("mark-thread", "state_updated", {"thread_id": thread_id, "status": status})


def command_show_state(config: dict[str, Any], thread_id: str | None) -> int:
    state_path = path_from_config(config, "state", "state_file")
    state = load_state(state_path)
    if thread_id:
        data = {"thread_id": thread_id, "thread": state.get("threads", {}).get(thread_id)}
    else:
        data = state
    return emit("show-state", "state_loaded", data)


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

    plan = sub.add_parser("plan-next-action", help="Plan the next Browser Relay action from a snapshot.")
    plan.add_argument("--current-url", default="", help="Current browser URL.")
    plan.add_argument("--snapshot-text", help="Raw snapshot text.")
    plan.add_argument("--snapshot-file", help="Snapshot text file.")
    plan.add_argument("--thread-id", help="Stable Boss thread ID if available.")
    plan.add_argument("--candidate-json", help="Optional candidate JSON override.")
    plan.add_argument("--candidate-file", help="Optional candidate JSON override file.")
    plan.add_argument("--target-candidate-name", help="Specific candidate to open from the thread list.")

    finalize = sub.add_parser("finalize-reply-plan", help="Turn llm-task JSON output into a sendable reply plan.")
    finalize.add_argument("--current-url", default="", help="Current browser URL.")
    finalize.add_argument("--snapshot-text", help="Raw snapshot text.")
    finalize.add_argument("--snapshot-file", help="Snapshot text file.")
    finalize.add_argument("--thread-id", help="Stable Boss thread ID if available.")
    finalize.add_argument("--candidate-json", help="Optional candidate JSON override.")
    finalize.add_argument("--candidate-file", help="Optional candidate JSON override file.")
    finalize.add_argument("--reply-result-json", help="Structured llm-task result JSON.")
    finalize.add_argument("--reply-result-file", help="Structured llm-task result JSON file.")
    finalize.add_argument("--allow-send", action="store_true", help="Include send-button browser actions.")
    finalize.add_argument("--include-resume", action="store_true", help="Include resume download actions when available.")

    resolve_cmd = sub.add_parser("resolve-download", help="Resolve the newest stable download from the download directory.")
    resolve_cmd.add_argument("--download-root", help="Override download directory.")
    resolve_cmd.add_argument("--known-files-json", help="JSON list of known file paths or names.")
    resolve_cmd.add_argument("--known-files-file", help="Path to JSON list of known file paths or names.")
    resolve_cmd.add_argument("--after-epoch", type=float, help="Only consider files modified after this Unix epoch.")

    mark = sub.add_parser("mark-thread", help="Mark thread status after a manual or browser step.")
    mark.add_argument("--thread-id", required=True, help="Stable Boss thread ID.")
    mark.add_argument("--status", required=True, help="Status to record.")
    mark.add_argument("--candidate-name", help="Candidate name.")
    mark.add_argument("--job-title", help="Job title.")
    mark.add_argument("--note", help="Optional note.")

    show = sub.add_parser("show-state", help="Read persisted runtime state.")
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

    if args.command == "plan-next-action":
        try:
            snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
        except (OSError, ValueError) as exc:
            return emit("plan-next-action", "snapshot_read_failed", {}, [str(exc)], ok=False)

        try:
            candidate_override = (
                load_json_arg(args.candidate_json, args.candidate_file, missing_message="Provide candidate JSON.")
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
            args.target_candidate_name,
        )

    if args.command == "finalize-reply-plan":
        try:
            snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
        except (OSError, ValueError) as exc:
            return emit("finalize-reply-plan", "snapshot_read_failed", {}, [str(exc)], ok=False)

        try:
            candidate_override = (
                load_json_arg(args.candidate_json, args.candidate_file, missing_message="Provide candidate JSON.")
                if args.candidate_json or args.candidate_file
                else None
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return emit("finalize-reply-plan", "candidate_read_failed", {}, [str(exc)], ok=False)

        try:
            reply_result = load_json_arg(
                args.reply_result_json,
                args.reply_result_file,
                missing_message="Provide --reply-result-json or --reply-result-file.",
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return emit("finalize-reply-plan", "reply_result_read_failed", {}, [str(exc)], ok=False)

        return command_finalize_reply_plan(
            config,
            args.current_url,
            snapshot_text,
            candidate_override,
            reply_result,
            args.thread_id,
            args.allow_send,
            args.include_resume,
        )

    if args.command == "resolve-download":
        return command_resolve_download(
            config,
            args.download_root,
            args.known_files_json,
            args.known_files_file,
            args.after_epoch,
        )

    if args.command == "mark-thread":
        return command_mark_thread(config, args.thread_id, args.status, args.candidate_name, args.job_title, args.note)

    if args.command == "show-state":
        return command_show_state(config, args.thread_id)

    return emit("bootstrap", "unknown_command", {}, [f"Unknown command: {args.command}"], ok=False)


if __name__ == "__main__":
    sys.exit(main())
