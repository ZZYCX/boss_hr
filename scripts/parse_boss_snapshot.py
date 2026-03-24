#!/usr/bin/env python3
"""Parse OpenClaw Browser Relay snapshot text for Boss chat pages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from validate_config import load_toml


QUOTED_REF_RE = re.compile(r'^\s*-\s*(?P<kind>[A-Za-z_]+)\s+"(?P<label>[^"]+)"\s+\[ref=(?P<ref>[^\]]+)\]:?$')
REF_ONLY_RE = re.compile(r"^\s*-\s*(?P<kind>[A-Za-z_]+)\s+\[ref=(?P<ref>[^\]]+)\]:?$")
TEXT_RE = re.compile(r"^\s*-\s*(?P<kind>[A-Za-z_]+):\s*(?P<text>.+)$")
EXPLICIT_FIELD_RE = {
    "candidate_name": re.compile(r"(?:姓名|候选人|牛人)\s*[:：]\s*(?P<value>.+)$"),
    "job_title": re.compile(r"(?:职位|岗位|投递岗位)\s*[:：]\s*(?P<value>.+)$"),
    "delivery_time": re.compile(r"(?:投递时间|消息时间|沟通时间|最近时间)\s*[:：]\s*(?P<value>.+)$"),
    "recent_message": re.compile(r"(?:最近消息|最新消息|消息内容|最近一条消息|发送的消息内容为)\s*[:：]\s*(?P<value>.+)$"),
}
JOB_HINTS = (
    "实习",
    "工程师",
    "开发",
    "运营",
    "销售",
    "设计",
    "产品",
    "数据",
    "标注",
    "算法",
    "后端",
    "前端",
    "测试",
    "主管",
    "经理",
)
TIME_RE = re.compile(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?|\d{1,2}:\d{2})")
UNREAD_COUNT_RE = re.compile(r"(?P<count>\d+)\s*条?未读")
THREAD_ITEM_TEXT_RE = re.compile(
    r"^\s*(?:(?P<unread>\d+)\s+)?(?P<time>(?:\d{1,2}:\d{2}|\d{2}-\d{2}|昨天|前天|今天|刚刚))\s+(?P<name>[\u4e00-\u9fffA-Za-z·0-9_-]{2,24})\s+(?P<job>\S+)(?:\s+(?P<message>.+))?$"
)
THREAD_HEADER_RE = re.compile(
    r"^\s*(?P<name>[\u4e00-\u9fffA-Za-z·0-9_-]{2,24})\s+(?:(?:刚刚|今天|昨日|昨天|前天)?活跃|在线|离线|\d{1,2}岁|应届生|本科|硕士|博士)"
)
JOB_TITLE_INLINE_RE = re.compile(
    r"(?:沟通职位|投递岗位|应聘职位|岗位|职位)\s*[:：]\s*(?P<value>.+?)(?:\s+期望[:：]|\s+\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}|$)"
)
THREAD_VIEW_ACTION_BAR_KEYWORDS = ("求简历", "换电话", "换微信", "约面试", "不合适", "发送")
THREAD_VIEW_META_KEYWORDS = ("刚刚活跃", "在线简历", "附件简历", "沟通职位")
NON_CANDIDATE_LABEL_KEYWORDS = ("职位", "管理", "沟通", "客服", "工具", "推荐", "搜索", "规范", "权益", "面试", "直播")
NOISE_TOKENS = (
    "step ",
    "任务执行结果",
    "执行详情",
    "当前执行操作",
    "是否找到",
    "是否成功",
    "最终结论",
    "消息已成功发送",
    "使用的选择器",
)


def load_snapshot_text(raw_text: str | None, text_file: str | None) -> str:
    if raw_text:
        return raw_text
    if text_file:
        return Path(text_file).read_text(encoding="utf-8-sig")
    raise ValueError("Provide --snapshot-text or --snapshot-file.")


def normalize_text(value: str) -> str:
    return value.casefold()


def node_display_text(node: dict[str, Any]) -> str:
    return str(node.get("label") or node.get("text") or "").strip()


def parse_nodes(snapshot_text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for index, line in enumerate(snapshot_text.splitlines()):
        if match := QUOTED_REF_RE.match(line):
            nodes.append(
                {
                    "index": index,
                    "kind": match.group("kind").lower(),
                    "label": match.group("label").strip(),
                    "ref": match.group("ref").strip(),
                    "raw": line.rstrip(),
                }
            )
            continue
        if match := REF_ONLY_RE.match(line):
            nodes.append(
                {
                    "index": index,
                    "kind": match.group("kind").lower(),
                    "label": "",
                    "ref": match.group("ref").strip(),
                    "raw": line.rstrip(),
                }
            )
            continue
        if match := TEXT_RE.match(line):
            nodes.append(
                {
                    "index": index,
                    "kind": match.group("kind").lower(),
                    "text": match.group("text").strip(),
                    "raw": line.rstrip(),
                }
            )
    return nodes


def contains_any(text: str, values: list[str]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(value) in normalized for value in values)


def search_box_index(nodes: list[dict[str, Any]], config: dict[str, Any]) -> int:
    labels = config["browser_actions"]["search_box_labels"]
    for node in nodes:
        if node.get("kind") != "textbox":
            continue
        if contains_any(node_display_text(node), labels):
            return int(node["index"])
    return -1


def nodes_after_search(nodes: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    index = search_box_index(nodes, config)
    if index < 0:
        return nodes
    return [node for node in nodes if int(node["index"]) > index]


def lines_after_search(nodes: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    return [node_display_text(node) for node in nodes_after_search(nodes, config) if node_display_text(node)]


def find_thread_detail_start_index(nodes: list[dict[str, Any]], config: dict[str, Any]) -> int:
    filtered = nodes_after_search(nodes, config)
    for node in filtered:
        text = node_display_text(node)
        if not text:
            continue
        match = THREAD_HEADER_RE.match(text)
        if not match:
            continue
        candidate_name = match.group("name").strip()
        if is_candidate_like_label(candidate_name, config):
            return int(node["index"])

    for node in filtered:
        text = node_display_text(node)
        if not text:
            continue
        if "沟通职位" in text or any(keyword in text for keyword in THREAD_VIEW_META_KEYWORDS):
            return int(node["index"])
        if sum(1 for keyword in THREAD_VIEW_ACTION_BAR_KEYWORDS if keyword in text) >= 3:
            return int(node["index"])

    return -1


def nodes_in_thread_detail(nodes: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    filtered = nodes_after_search(nodes, config)
    start_index = find_thread_detail_start_index(nodes, config)
    if start_index < 0:
        return filtered
    return [node for node in filtered if int(node["index"]) >= start_index]


def ignored_labels(config: dict[str, Any]) -> set[str]:
    browser_actions = config["browser_actions"]
    labels = (
        browser_actions["ignored_thread_labels"]
        + browser_actions["conversation_filter_labels"]
        + browser_actions["search_box_labels"]
        + browser_actions["send_button_texts"]
        + browser_actions["no_contacts_texts"]
        + browser_actions["no_message_texts"]
    )
    return {value.strip() for value in labels if value.strip()}


def escape_playwright_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def build_thread_selector_candidates(label: str, config: dict[str, Any]) -> list[str]:
    browser_actions = config["browser_actions"]
    escaped = escape_playwright_text(label)
    selectors = [f'{selector}:has-text("{escaped}")' for selector in browser_actions["thread_open_container_selectors"]]
    selectors.append(f'text="{escaped}"')
    selectors.append(f'xpath=//*[normalize-space()="{escaped}"]/ancestor::*[@role="listitem" or self::li][1]')
    return unique_strings(selectors)


def build_thread_js_fallback(label: str, config: dict[str, Any]) -> str:
    browser_actions = config["browser_actions"]
    return "\n".join(
        [
            "(() => {",
            f"  const targetText = {json.dumps(label, ensure_ascii=False)};",
            (
                "  const containerSelectors = "
                + json.dumps(browser_actions["thread_open_container_selectors"], ensure_ascii=False)
                + ";"
            ),
            '  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();',
            "  const seen = new Set();",
            "  const containers = [];",
            "  for (const selector of containerSelectors) {",
            "    for (const node of document.querySelectorAll(selector)) {",
            "      if (seen.has(node)) continue;",
            "      seen.add(node);",
            "      containers.push({ selector, node });",
            "    }",
            "  }",
            "  for (const item of containers) {",
            "    const text = normalize(item.node.innerText || item.node.textContent);",
            "    if (text !== targetText && !text.includes(targetText)) continue;",
            "    item.node.click();",
            "    return { ok: true, strategy: `container:${item.selector}`, matchedText: text };",
            "  }",
            "  return { ok: false, reason: 'thread-container-not-found', candidateName: targetText };",
            "})()",
        ]
    )


def is_candidate_like_label(label: str, config: dict[str, Any]) -> bool:
    if not label:
        return False
    if label in ignored_labels(config):
        return False
    if any(keyword in label for keyword in NON_CANDIDATE_LABEL_KEYWORDS):
        return False
    if contains_any(label, config["reply"]["resume_trigger_keywords"]):
        return False
    if len(label) < 2 or len(label) > 24:
        return False
    if any(symbol in label for symbol in (":", "/", "?", "#", "[", "]", "http")):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z·0-9_-]{2,24}", label))


def extract_thread_refs(nodes: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    thread_nodes = nodes_after_search(nodes, config)
    refs: list[dict[str, Any]] = []
    for position, node in enumerate(thread_nodes):
        if node.get("kind") not in {"link", "listitem"}:
            continue
        ref = node.get("ref")
        label = node.get("label")
        if not ref and not label and node.get("kind") == "listitem":
            row = parse_thread_listitem_text(node_display_text(node), config)
            if row:
                refs.append(
                    {
                        "ref": "",
                        "label": row["candidate_name"],
                        "kind": node["kind"],
                        "has_unread": row["has_unread"],
                        "unread_count": row["unread_count"],
                        "delivery_time": row["delivery_time"],
                        "job_title": row["job_title"],
                        "latest_message": row["latest_message"],
                        "selector_candidates": build_thread_selector_candidates(row["candidate_name"], config),
                        "js_click_fallback": build_thread_js_fallback(row["candidate_name"], config),
                    }
                )
            continue
        if not ref or not label:
            continue
        if is_candidate_like_label(label, config):
            previous_candidate_position = -1
            for earlier_position in range(position - 1, -1, -1):
                earlier_label = str(thread_nodes[earlier_position].get("label", "")).strip()
                if earlier_label and is_candidate_like_label(earlier_label, config):
                    previous_candidate_position = earlier_position
                    break
            next_candidate_position = len(thread_nodes)
            for later_position in range(position + 1, len(thread_nodes)):
                later_label = str(thread_nodes[later_position].get("label", "")).strip()
                if later_label and is_candidate_like_label(later_label, config):
                    next_candidate_position = later_position
                    break
            before_context_start = max(previous_candidate_position + 1, position - 2)
            after_context_end = min(next_candidate_position, position + 4)
            unread_texts = [
                node_display_text(item)
                for item in list(thread_nodes[before_context_start:position]) + list(thread_nodes[position + 1 : after_context_end])
                if node_display_text(item)
            ]
            unread_count = 0
            has_unread = False
            for text in unread_texts:
                if match := UNREAD_COUNT_RE.search(text):
                    unread_count = int(match.group("count"))
                    has_unread = True
                    break
                if text.isdigit():
                    unread_count = int(text)
                    has_unread = unread_count > 0
                    if has_unread:
                        break
                if contains_any(text, config["browser_actions"]["unread_marker_keywords"]):
                    has_unread = True
            refs.append(
                {
                    "ref": ref,
                    "label": label,
                    "kind": node["kind"],
                    "has_unread": has_unread,
                    "unread_count": unread_count,
                    "selector_candidates": build_thread_selector_candidates(label, config),
                    "js_click_fallback": build_thread_js_fallback(label, config),
                }
            )

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in refs:
        key = (item["ref"], item["label"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def parse_thread_listitem_text(text: str, config: dict[str, Any]) -> dict[str, Any] | None:
    match = THREAD_ITEM_TEXT_RE.match(text.strip())
    if not match:
        return None

    candidate_name = match.group("name").strip()
    if not is_candidate_like_label(candidate_name, config):
        return None

    unread_value = match.group("unread")
    unread_count = int(unread_value) if unread_value else 0
    return {
        "candidate_name": candidate_name,
        "delivery_time": match.group("time").strip(),
        "job_title": match.group("job").strip(),
        "latest_message": str(match.group("message") or "").strip(),
        "has_unread": unread_count > 0,
        "unread_count": unread_count,
    }


def extract_candidate_name_from_nodes(nodes: list[dict[str, Any]], config: dict[str, Any]) -> str:
    for node in nodes:
        text = node_display_text(node)
        if not text:
            continue
        match = THREAD_HEADER_RE.match(text)
        if match:
            candidate_name = match.group("name").strip()
            if is_candidate_like_label(candidate_name, config):
                return candidate_name

    for node in nodes:
        label = node_display_text(node)
        if not label or not is_candidate_like_label(label, config):
            continue
        if node.get("kind") in {"heading", "link", "button", "text", "listitem"}:
            return label
    return ""


def extract_explicit_field(lines: list[str], field_name: str) -> str:
    pattern = EXPLICIT_FIELD_RE[field_name]
    for line in lines:
        if match := pattern.search(line):
            value = match.group("value").strip()
            if value:
                return value
    return ""


def extract_job_title(lines: list[str], config: dict[str, Any]) -> str:
    for line in lines:
        if match := JOB_TITLE_INLINE_RE.search(line):
            return match.group("value").strip()

    explicit = extract_explicit_field(lines, "job_title")
    if explicit:
        return re.split(r"\s+期望[:：]", explicit, maxsplit=1)[0].strip()

    ignored = ignored_labels(config)
    for line in lines:
        if line in ignored:
            continue
        if any(hint in line for hint in JOB_HINTS) and len(line) <= 40:
            return line
    return ""


def extract_delivery_time(lines: list[str]) -> str:
    explicit = extract_explicit_field(lines, "delivery_time")
    if explicit:
        return explicit

    for line in lines:
        if match := TIME_RE.search(line):
            return match.group(1)
    return ""


def extract_recent_messages(lines: list[str], config: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    explicit = extract_explicit_field(lines, "recent_message")
    if explicit:
        messages.append({"role": "candidate", "text": explicit})

    if messages:
        return messages

    ignored = ignored_labels(config)
    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            continue
        if "沟通职位" in cleaned:
            if match := TIME_RE.search(cleaned):
                candidate_message = cleaned[match.end() :].strip()
                if candidate_message:
                    messages.append({"role": "candidate", "text": candidate_message})
            continue
        for keyword in THREAD_VIEW_ACTION_BAR_KEYWORDS:
            token = f" {keyword}"
            index = cleaned.find(token)
            if index > 0:
                cleaned = cleaned[:index].strip()
                break

        normalized = normalize_text(cleaned)
        if cleaned in ignored:
            continue
        if any(token in normalized for token in NOISE_TOKENS):
            continue
        if len(cleaned) < 8:
            continue
        if any(cleaned.startswith(prefix) for prefix in ("姓名", "职位", "最近消息", "投递时间", "消息时间")):
            continue
        if THREAD_HEADER_RE.match(cleaned):
            continue
        if "沟通职位" in cleaned:
            continue
        if cleaned in {"工作经历", "未填写工作经历"}:
            continue
        if re.fullmatch(r"\d{4}-\d{4}", cleaned):
            continue
        if "·" in cleaned and any(keyword in cleaned for keyword in ("本科", "硕士", "博士", "大专")):
            continue
        if "已读" in cleaned:
            continue
        messages.append({"role": "candidate", "text": cleaned})

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for message in messages:
        text = message["text"]
        if text in seen:
            continue
        seen.add(text)
        deduped.append(message)
    max_recent_messages = int(config.get("reply", {}).get("max_recent_messages", 3))
    if max_recent_messages <= 0:
        max_recent_messages = 3
    return deduped[-max_recent_messages:]


def build_ref_target_block(refs: list[dict[str, str]], selectors: list[str]) -> dict[str, Any]:
    return {"refs": refs, "selectors": selectors}


def find_labeled_targets(
    nodes: list[dict[str, Any]],
    config: dict[str, Any],
    labels: list[str],
    allowed_kinds: set[str] | None = None,
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    kinds = allowed_kinds or {"button", "link"}
    for node in nodes_after_search(nodes, config):
        label = node_display_text(node)
        if not label or not node.get("ref"):
            continue
        if node.get("kind") not in kinds:
            continue
        if contains_any(label, labels):
            refs.append({"ref": node["ref"], "label": label})
    return refs


def find_reply_input_targets(nodes: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    refs: list[dict[str, str]] = []
    search_labels = config["browser_actions"]["search_box_labels"]
    for node in nodes_after_search(nodes, config):
        if node.get("kind") != "textbox":
            continue
        label = node_display_text(node)
        if label and contains_any(label, search_labels):
            continue
        if node.get("ref"):
            refs.append({"ref": node["ref"], "label": label})

    return {
        "refs": refs,
        "selectors": config["browser_actions"]["reply_input_selectors"],
    }


def find_send_targets(nodes: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    refs: list[dict[str, str]] = []
    button_texts = config["browser_actions"]["send_button_texts"]
    for node in nodes_after_search(nodes, config):
        label = node_display_text(node)
        if not label or not node.get("ref"):
            continue
        if contains_any(label, button_texts):
            refs.append({"ref": node["ref"], "label": label})

    return {
        "refs": refs,
        "selectors": config["browser_actions"]["send_button_selectors"],
    }


def find_resume_targets(nodes: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    keywords = config["reply"]["resume_trigger_keywords"]
    for node in nodes:
        label = node_display_text(node)
        if not label:
            continue
        if sum(1 for keyword in THREAD_VIEW_ACTION_BAR_KEYWORDS if keyword in label) >= 2:
            continue
        if contains_any(label, keywords):
            targets.append(
                {
                    "ref": node.get("ref", ""),
                    "label": label,
                    "kind": node.get("kind", ""),
                    "selectors": config["browser_actions"]["resume_download_selectors"],
                }
            )
    return targets


def build_resume_workflow(
    nodes: list[dict[str, Any]],
    config: dict[str, Any],
    visible_text: str,
    resume_targets: list[dict[str, Any]],
) -> dict[str, Any]:
    browser_actions = config["browser_actions"]
    accept_refs = find_labeled_targets(nodes, config, browser_actions["resume_accept_texts"], {"button", "link"})
    preview_refs = find_labeled_targets(nodes, config, browser_actions["resume_preview_texts"], {"button", "link"})
    accept_detected = bool(accept_refs) or contains_any(visible_text, browser_actions["resume_accept_texts"])
    preview_detected = bool(preview_refs) or contains_any(visible_text, browser_actions["resume_preview_texts"])
    detected = bool(resume_targets or accept_refs or preview_refs or contains_any(visible_text, config["reply"]["resume_trigger_keywords"]))
    missing_required_steps: list[str] = []
    if detected and not accept_detected:
        missing_required_steps.append("resume_accept")

    return {
        "resume_detected": detected,
        "accept_detected": accept_detected,
        "preview_detected": preview_detected,
        "accept_button": build_ref_target_block(accept_refs, browser_actions["resume_accept_selectors"]),
        "preview_button": build_ref_target_block(preview_refs, browser_actions["resume_preview_selectors"]),
        "preview_modal": {"selectors": browser_actions["resume_preview_modal_selectors"]},
        "download_trigger": {
            "selectors": browser_actions["resume_download_selectors"],
            "icon_selectors": browser_actions["resume_download_icon_selectors"],
            "host_selectors": browser_actions["resume_download_host_selectors"],
        },
        "close_button": {"selectors": browser_actions["resume_preview_close_selectors"]},
        "missing_required_steps": missing_required_steps,
    }


def classify_page_kind(
    current_url: str,
    lines: list[str],
    thread_refs: list[dict[str, Any]],
    input_targets: dict[str, Any],
    send_targets: dict[str, Any],
    resume_targets: list[dict[str, Any]],
    resume_workflow: dict[str, Any],
    candidate_name: str,
    recent_messages: list[dict[str, str]],
    config: dict[str, Any],
) -> str:
    browser_actions = config["browser_actions"]
    joined = "\n".join(lines)
    has_thread_view_text_signals = any(
        THREAD_HEADER_RE.match(line)
        or "沟通职位" in line
        or any(keyword in line for keyword in THREAD_VIEW_META_KEYWORDS)
        or sum(1 for keyword in THREAD_VIEW_ACTION_BAR_KEYWORDS if keyword in line) >= 3
        for line in lines
    )

    if contains_any(joined, browser_actions["no_contacts_texts"]) or contains_any(joined, browser_actions["no_message_texts"]):
        return "chat_empty"

    has_thread_view_signals = bool(
        input_targets["refs"]
        or send_targets["refs"]
        or resume_targets
        or resume_workflow["accept_button"]["refs"]
        or resume_workflow["preview_button"]["refs"]
        or resume_workflow.get("accept_detected")
        or resume_workflow.get("preview_detected")
        or has_thread_view_text_signals
        or (candidate_name and not thread_refs)
    )
    if has_thread_view_signals and "chat" in current_url:
        return "thread_view"

    if "chat" in current_url:
        if thread_refs:
            return "chat_list"
        return "chat_list"
    return "unknown"


def parse_snapshot(config: dict[str, Any], current_url: str, snapshot_text: str) -> dict[str, Any]:
    nodes = parse_nodes(snapshot_text)
    lines = lines_after_search(nodes, config)
    detail_nodes = nodes_in_thread_detail(nodes, config)
    detail_lines = [node_display_text(node) for node in detail_nodes if node_display_text(node)]
    visible_text = "\n".join(unique_strings([node_display_text(node) for node in nodes if node_display_text(node)]))
    detail_visible_text = "\n".join(unique_strings(detail_lines))

    thread_refs = extract_thread_refs(nodes, config)
    job_title = extract_job_title(detail_lines, config)
    delivery_time = extract_delivery_time(detail_lines)
    recent_messages = extract_recent_messages(detail_lines, config)
    input_targets = find_reply_input_targets(detail_nodes, config)
    send_targets = find_send_targets(detail_nodes, config)
    resume_targets = find_resume_targets(detail_nodes, config)
    explicit_name = extract_explicit_field(detail_lines, "candidate_name")
    thread_view_candidate_name = extract_candidate_name_from_nodes(detail_nodes, config)
    resume_workflow = build_resume_workflow(detail_nodes, config, detail_visible_text, resume_targets)
    has_thread_view_signals = bool(
        input_targets["refs"]
        or send_targets["refs"]
        or resume_targets
        or resume_workflow["accept_button"]["refs"]
        or resume_workflow["preview_button"]["refs"]
        or resume_workflow.get("accept_detected")
        or resume_workflow.get("preview_detected")
        or explicit_name
    )
    candidate_name = explicit_name
    if not candidate_name and has_thread_view_signals:
        candidate_name = thread_view_candidate_name
    if not candidate_name and len(thread_refs) == 1:
        candidate_name = thread_refs[0]["label"]

    page_kind = classify_page_kind(
        current_url,
        detail_lines,
        thread_refs,
        input_targets,
        send_targets,
        resume_targets,
        resume_workflow,
        candidate_name,
        recent_messages,
        config,
    )

    candidate: dict[str, Any] = {
        "thread_id": "",
        "candidate_name": candidate_name,
        "job_title": job_title,
        "delivery_time": delivery_time,
        "company_name": config["reply"]["default_company_name"],
        "has_resume": resume_workflow["resume_detected"],
        "resume_hint": resume_targets[0]["label"] if resume_targets else "",
        "recent_messages": recent_messages,
    }

    return {
        "page_kind": page_kind,
        "current_url": current_url,
        "visible_text": visible_text,
        "detail_visible_text": detail_visible_text,
        "thread_refs": thread_refs,
        "candidate": candidate,
        "reply_input": input_targets,
        "send_button": send_targets,
        "resume_targets": resume_targets,
        "resume_workflow": resume_workflow,
        "signals": {
            "has_no_contacts": contains_any(visible_text, config["browser_actions"]["no_contacts_texts"]),
            "has_no_message": contains_any(visible_text, config["browser_actions"]["no_message_texts"]),
            "has_resume": resume_workflow["resume_detected"],
            "thread_ref_count": len(thread_refs),
            "unread_thread_count": len([item for item in thread_refs if item.get("has_unread")]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse OpenClaw Browser Relay snapshot text for Boss pages.")
    parser.add_argument("--config", required=True, help="Path to TOML config.")
    parser.add_argument("--current-url", default="", help="Current page URL.")
    parser.add_argument("--snapshot-text", help="Raw snapshot text.")
    parser.add_argument("--snapshot-file", help="Snapshot text file.")
    args = parser.parse_args()

    try:
        config = load_toml(Path(args.config))
        snapshot_text = load_snapshot_text(args.snapshot_text, args.snapshot_file)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    analysis = parse_snapshot(config, args.current_url, snapshot_text)
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
