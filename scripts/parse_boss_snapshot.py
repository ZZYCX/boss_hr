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
    selectors = [f'{selector}:has-text("{escaped}")' for selector in browser_actions["thread_item_container_selectors"]]
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
                + json.dumps(browser_actions["thread_item_container_selectors"], ensure_ascii=False)
                + ";"
            ),
            (
                "  const clickableSelectors = "
                + json.dumps(browser_actions["thread_item_clickable_ancestor_selectors"], ensure_ascii=False)
                + ";"
            ),
            '  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();',
            "  const nodes = Array.from(document.querySelectorAll('body *')).filter((el) => {",
            "    const text = normalize(el.textContent);",
            "    return text === targetText || text.includes(targetText);",
            "  });",
            "  for (const node of nodes) {",
            "    for (const selector of containerSelectors) {",
            "      const container = node.closest(selector);",
            "      if (container) {",
            "        container.click();",
            "        return { ok: true, strategy: `container:${selector}` };",
            "      }",
            "    }",
            "    for (const selector of clickableSelectors) {",
            "      const clickable = node.closest(selector);",
            "      if (clickable) {",
            "        clickable.click();",
            "        return { ok: true, strategy: `ancestor:${selector}` };",
            "      }",
            "    }",
            "  }",
            "  return { ok: false, reason: 'no-clickable-thread-container' };",
            "})()",
        ]
    )


def is_candidate_like_label(label: str, config: dict[str, Any]) -> bool:
    if not label:
        return False
    if label in ignored_labels(config):
        return False
    if contains_any(label, config["reply"]["resume_trigger_keywords"]):
        return False
    if len(label) < 2 or len(label) > 24:
        return False
    if any(symbol in label for symbol in (":", "/", "?", "#", "[", "]", "http")):
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z·0-9_-]{2,24}", label))


def extract_thread_refs(nodes: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for node in nodes_after_search(nodes, config):
        if node.get("kind") not in {"link", "listitem"}:
            continue
        ref = node.get("ref")
        label = node.get("label")
        if not ref or not label:
            continue
        if is_candidate_like_label(label, config):
            refs.append(
                {
                    "ref": ref,
                    "label": label,
                    "kind": node["kind"],
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


def extract_explicit_field(lines: list[str], field_name: str) -> str:
    pattern = EXPLICIT_FIELD_RE[field_name]
    for line in lines:
        if match := pattern.search(line):
            value = match.group("value").strip()
            if value:
                return value
    return ""


def extract_job_title(lines: list[str], config: dict[str, Any]) -> str:
    explicit = extract_explicit_field(lines, "job_title")
    if explicit:
        return explicit

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
        normalized = normalize_text(line)
        if line in ignored:
            continue
        if any(token in normalized for token in NOISE_TOKENS):
            continue
        if len(line) < 8:
            continue
        if any(line.startswith(prefix) for prefix in ("姓名", "职位", "最近消息", "投递时间", "消息时间")):
            continue
        messages.append({"role": "candidate", "text": line})

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for message in messages:
        text = message["text"]
        if text in seen:
            continue
        seen.add(text)
        deduped.append(message)
    return deduped[-3:]


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
    for node in nodes_after_search(nodes, config):
        label = node_display_text(node)
        if not label:
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


def classify_page_kind(
    current_url: str,
    lines: list[str],
    thread_refs: list[dict[str, Any]],
    input_targets: dict[str, Any],
    send_targets: dict[str, Any],
    resume_targets: list[dict[str, Any]],
    candidate_name: str,
    recent_messages: list[dict[str, str]],
    config: dict[str, Any],
) -> str:
    browser_actions = config["browser_actions"]
    joined = "\n".join(lines)

    if contains_any(joined, browser_actions["no_contacts_texts"]) or contains_any(joined, browser_actions["no_message_texts"]):
        return "chat_empty"

    has_thread_view_signals = bool(
        candidate_name
        or recent_messages
        or input_targets["refs"]
        or send_targets["refs"]
        or resume_targets
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
    visible_text = "\n".join(unique_strings([node_display_text(node) for node in nodes if node_display_text(node)]))

    thread_refs = extract_thread_refs(nodes, config)
    explicit_name = extract_explicit_field(lines, "candidate_name")
    candidate_name = explicit_name or (thread_refs[0]["label"] if len(thread_refs) == 1 else "")
    job_title = extract_job_title(lines, config)
    delivery_time = extract_delivery_time(lines)
    recent_messages = extract_recent_messages(lines, config)
    input_targets = find_reply_input_targets(nodes, config)
    send_targets = find_send_targets(nodes, config)
    resume_targets = find_resume_targets(nodes, config)
    page_kind = classify_page_kind(
        current_url,
        lines,
        thread_refs,
        input_targets,
        send_targets,
        resume_targets,
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
        "has_resume": bool(resume_targets),
        "resume_hint": resume_targets[0]["label"] if resume_targets else "",
        "recent_messages": recent_messages,
    }

    return {
        "page_kind": page_kind,
        "current_url": current_url,
        "visible_text": visible_text,
        "thread_refs": thread_refs,
        "candidate": candidate,
        "reply_input": input_targets,
        "send_button": send_targets,
        "resume_targets": resume_targets,
        "signals": {
            "has_no_contacts": contains_any(visible_text, config["browser_actions"]["no_contacts_texts"]),
            "has_no_message": contains_any(visible_text, config["browser_actions"]["no_message_texts"]),
            "has_resume": bool(resume_targets),
            "thread_ref_count": len(thread_refs),
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
