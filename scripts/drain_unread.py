#!/usr/bin/env python3
"""Batch-drain unread Boss conversations via repeated OpenClaw agent turns."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomllib

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local editing only.
    fcntl = None


DEFAULT_GATEWAY_PORT = 18789
DEFAULT_AGENT_ID = "main"
DEFAULT_HTTP_TIMEOUT_SECONDS = 600
DEFAULT_MAX_TOKENS = 2200
DEFAULT_MODEL_PREFIX = "openclaw"
DEFAULT_BATCH_LOG = "drain-unread-runs.jsonl"
DEFAULT_LOCK_FILE = "drain-unread.lock"
RESUME_DOWNLOAD_VALUES = {"not_applicable", "downloaded", "skipped", "failed"}
FATAL_ERROR_STEPS = {"preflight", "session-check", "session_check", "session", "bootstrap", "environment"}
FATAL_ERROR_REASONS = {
    "gateway_unreachable",
    "invalid_json_output",
    "no_attached_tab",
    "wrong_page",
    "login_required",
    "risk_control",
    "lock_conflict",
}
FATAL_ERROR_KEYWORDS = (
    "no attached",
    "attach",
    "wrong page",
    "boss 沟通页面",
    "登录",
    "扫码登录",
    "risk",
    "风控",
    "gateway",
)
OPENCLAW_CONFIG_CANDIDATES = (
    "/home/node/.openclaw/openclaw.json",
    "~/.openclaw/openclaw.json",
    "/data/openclaw-data/config/openclaw.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload must be an object: {path}")
    return payload


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def expand_raw_command(argv: list[str]) -> list[str]:
    expanded: list[str] = []
    index = 0
    while index < len(argv):
        current = argv[index]
        if current == "--raw-command":
            if index + 1 >= len(argv):
                raise SystemExit("--raw-command requires a value.")
            expanded.extend(shlex.split(argv[index + 1]))
            index += 2
            continue
        expanded.append(current)
        index += 1
    return expanded


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drain unread Boss conversations via OpenClaw agent turns.")
    parser.add_argument("--config", required=True, help="Path to boss-hr-assistant TOML config.")
    parser.add_argument("--openclaw-config", help="Path to openclaw.json.")
    parser.add_argument("--gateway-url", help="Gateway base URL, for example http://127.0.0.1:18789.")
    parser.add_argument("--gateway-token", help="Gateway auth token.")
    parser.add_argument("--default-agent-id", default=DEFAULT_AGENT_ID, help="Fallback OpenClaw agent id.")
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        help="Timeout for each agent turn HTTP request.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    drain = sub.add_parser("drain-unread", help="Drain unread Boss conversations.")
    drain.add_argument("--profile", help="Browser profile name.")
    drain.add_argument("--agent-id", help="Explicit agent id.")
    drain.add_argument("--allow-send", action="store_true", help="Allow real sends.")
    drain.add_argument("--max-rounds", type=int, help="Override max_unread_threads_per_run.")

    return parser.parse_args(expand_raw_command(argv))


def normalize_url(base: str) -> str:
    trimmed = base.strip().rstrip("/")
    if not trimmed:
        raise ValueError("Gateway URL cannot be blank.")
    if trimmed.startswith("http://") or trimmed.startswith("https://"):
        return trimmed
    return f"http://{trimmed}"


def resolve_openclaw_config_path(explicit: str | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_path = os.environ.get("OPENCLAW_CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.extend(Path(item).expanduser() for item in OPENCLAW_CONFIG_CANDIDATES)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_openclaw_config(explicit: str | None) -> tuple[Path | None, dict[str, Any]]:
    path = resolve_openclaw_config_path(explicit)
    if path is None:
        return None, {}
    return path, load_json(path)


def resolve_gateway_url(args: argparse.Namespace, openclaw_config: dict[str, Any]) -> str:
    if args.gateway_url:
        return normalize_url(args.gateway_url)

    gateway_config = openclaw_config.get("gateway", {})
    port = int(gateway_config.get("port") or DEFAULT_GATEWAY_PORT)
    return f"http://127.0.0.1:{port}"


def resolve_gateway_token(args: argparse.Namespace, openclaw_config: dict[str, Any]) -> str:
    if args.gateway_token:
        return args.gateway_token

    env_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if env_token:
        return env_token

    token = openclaw_config.get("gateway", {}).get("auth", {}).get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    raise ValueError("Gateway token is required.")


def resolve_browser_profile(args: argparse.Namespace, skill_config: dict[str, Any], openclaw_config: dict[str, Any]) -> str:
    if getattr(args, "profile", None):
        return str(args.profile).strip()

    openclaw_section = skill_config.get("openclaw", {})
    preferred = openclaw_section.get("preferred_browser_profile")
    if isinstance(preferred, str) and preferred.strip():
        return preferred.strip()

    default_profile = openclaw_config.get("browser", {}).get("defaultProfile")
    if isinstance(default_profile, str) and default_profile.strip():
        return default_profile.strip()

    fallback = openclaw_section.get("fallback_browser_profile")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()

    raise ValueError("Unable to resolve browser profile.")


def resolve_agent_id(args: argparse.Namespace) -> str:
    explicit = getattr(args, "agent_id", None)
    if explicit:
        return str(explicit).strip()
    return str(args.default_agent_id or DEFAULT_AGENT_ID).strip()


def resolve_max_rounds(args: argparse.Namespace, skill_config: dict[str, Any]) -> int:
    if getattr(args, "max_rounds", None):
        return int(args.max_rounds)
    return int(skill_config["boss"]["max_unread_threads_per_run"])


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def derive_state_dir(skill_config_path: Path, skill_config: dict[str, Any]) -> Path:
    configured = Path(skill_config["state"]["state_file"]).parent
    try:
        ensure_dir(configured)
        return configured
    except OSError:
        fallback = skill_config_path.resolve().parents[1] / "runtime" / "state"
        ensure_dir(fallback)
        return fallback


@contextmanager
def single_instance_lock(lock_path: Path) -> Any:
    ensure_dir(lock_path.parent)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        else:  # pragma: no cover - Windows fallback for local editing only.
            if lock_path.exists() and lock_path.stat().st_size > 0:
                raise BlockingIOError("Lock already held.")
            acquired = True
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "started_at": utc_now()}, ensure_ascii=False))
        handle.flush()
        yield
    finally:
        if acquired and fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def remove_code_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = remove_code_fences(text)
    if not cleaned:
        raise ValueError("Assistant output is empty.")
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Assistant output does not contain a JSON object.") from None
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Assistant output JSON must be an object.")
    return payload


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def normalize_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def normalize_round_result(payload: dict[str, Any]) -> dict[str, Any]:
    action = normalize_string(payload.get("action")) or "error"
    if action not in {"processed", "wait_for_unread", "error"}:
        action = "error"

    resume_download = normalize_string(payload.get("resume_download")) or "not_applicable"
    if resume_download not in RESUME_DOWNLOAD_VALUES:
        resume_download = "not_applicable"

    return {
        "action": action,
        "detected_unread_count": normalize_int(payload.get("detected_unread_count")),
        "candidate_name": normalize_string(payload.get("candidate_name")),
        "opened": normalize_bool(payload.get("opened")),
        "llm_reply_text": normalize_string(payload.get("llm_reply_text")),
        "final_reply_text": normalize_string(payload.get("final_reply_text")),
        "sent": normalize_bool(payload.get("sent")),
        "resume_download": resume_download,
        "error_step": normalize_string(payload.get("error_step")),
        "error_reason": normalize_string(payload.get("error_reason")),
    }


def build_single_round_prompt(profile: str, allow_send: bool, skip_candidates: list[str]) -> str:
    skip_block = json.dumps(skip_candidates, ensure_ascii=False) if skip_candidates else "[]"
    schema = {
        "action": "processed|wait_for_unread|error",
        "detected_unread_count": 0,
        "candidate_name": "",
        "opened": False,
        "llm_reply_text": "",
        "final_reply_text": "",
        "sent": False,
        "resume_download": "not_applicable|downloaded|skipped|failed",
        "error_step": "",
        "error_reason": "",
    }

    return "\n".join(
        [
            "你是 Boss-hr 的执行助手。",
            "必须实时使用 $boss-hr-assistant 处理当前已 attach 的 Boss 招聘沟通页面。",
            "本轮只处理一个未读会话，不要输出进度汇报，不要输出自然语言总结，只能输出一个 JSON 对象。",
            f'浏览器 profile 固定为 "{profile}"。',
            f"allow_send 固定为 {bool_text(allow_send)}。",
            f"本批次需要跳过的候选人名单: {skip_block}。",
            "如果左侧未读列表里只剩下需要跳过的候选人，则返回 action=wait_for_unread。",
            "第一动作必须实时检查当前已 attach 页面快照，不要根据历史对话猜测浏览器状态。",
            "如果没有已 attach 标签页，返回 action=error, error_step=preflight, error_reason=no_attached_tab。",
            "如果当前页面不是 Boss 沟通页，返回 action=error, error_step=preflight, error_reason=wrong_page。",
            "如果检测到登录失效，返回 action=error, error_step=preflight, error_reason=login_required。",
            "如果检测到风控/安全验证，返回 action=error, error_step=preflight, error_reason=risk_control。",
            "只允许处理一个未读会话；如果有 skip 名单，从未读里选择第一个不在 skip 名单中的候选人。",
            "必须严格使用这个链路：session-check -> plan-next-action -> open_thread -> 重新 snapshot -> plan-next-action -> llm-task -> finalize-reply-plan -> browser_actions/post_actions。",
            "不允许手工编写回复，不允许绕过 llm-task，不允许在 llm-task 失败后自行补发消息。",
            f"finalize-reply-plan 时 allow_send 必须为 {bool_text(allow_send)}。",
            "如果当前会话检测到附件简历入口，则 finalize-reply-plan 时 include_resume=true，并按固定下载流程执行。",
            "如果简历入口是 disabled 或当前无附件简历，则 resume_download 返回 skipped 或 not_applicable，不要伪造下载成功。",
            "错误步骤只允许使用：preflight, open_thread, llm_task, finalize_reply_plan, send, resume_download。",
            "error_reason 优先使用这些稳定值：no_attached_tab, wrong_page, login_required, risk_control, open_thread_failed, llm_task_failed, finalize_reply_plan_failed, send_failed, resume_download_failed。",
            "返回的 JSON schema 必须严格匹配下方字段，不允许增加字段，不允许 Markdown code fence：",
            json.dumps(schema, ensure_ascii=False),
        ]
    )


def http_json(url: str, token: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gateway request failed: {exc.reason}") from exc


def extract_assistant_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Gateway response missing choices.")
    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        raise ValueError("Gateway response message is invalid.")
    content = message.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts).strip()
    if isinstance(content, str):
        return content.strip()
    raise ValueError("Gateway response content is invalid.")


def invoke_single_turn(
    gateway_url: str,
    gateway_token: str,
    agent_id: str,
    prompt: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    payload = {
        "model": f"{DEFAULT_MODEL_PREFIX}:{agent_id}",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    response = http_json(f"{gateway_url}/v1/chat/completions", gateway_token, payload, timeout_seconds)
    response_text = extract_assistant_content(response)
    parsed = extract_json_object(response_text)
    return normalize_round_result(parsed)


def classify_fatal(round_result: dict[str, Any]) -> bool:
    step = normalize_string(round_result.get("error_step")).casefold()
    reason = normalize_string(round_result.get("error_reason")).casefold()
    if step in FATAL_ERROR_STEPS or reason in FATAL_ERROR_REASONS:
        return True
    return any(keyword in reason for keyword in FATAL_ERROR_KEYWORDS)


def fatal_result(
    step: str,
    reason: str,
    *,
    profile: str,
    agent_id: str,
    max_rounds: int,
    started_at: str,
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ended_at = utc_now()
    started_ts = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    ended_ts = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    return {
        "ok": False,
        "action": "fatal_error",
        "profile": profile,
        "agent_id": agent_id,
        "max_rounds": max_rounds,
        "processed_count": sum(1 for item in (results or []) if item.get("action") == "processed"),
        "error_count": sum(1 for item in (results or []) if item.get("action") == "error"),
        "results": results or [],
        "fatal_error": {"step": step, "reason": reason},
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round((ended_ts - started_ts).total_seconds(), 3),
    }


def run_drain_unread(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).expanduser()
    skill_config = load_toml(config_path)
    openclaw_config_path, openclaw_config = resolve_openclaw_config(args.openclaw_config)

    profile = resolve_browser_profile(args, skill_config, openclaw_config)
    agent_id = resolve_agent_id(args)
    max_rounds = resolve_max_rounds(args, skill_config)
    gateway_url = resolve_gateway_url(args, openclaw_config)
    gateway_token = resolve_gateway_token(args, openclaw_config)

    state_dir = derive_state_dir(config_path, skill_config)
    lock_path = state_dir / DEFAULT_LOCK_FILE
    batch_log_path = state_dir / DEFAULT_BATCH_LOG
    started_at = utc_now()
    results: list[dict[str, Any]] = []
    skip_candidates: list[str] = []
    skip_seen: set[str] = set()

    try:
        with single_instance_lock(lock_path):
            for _ in range(max_rounds):
                prompt = build_single_round_prompt(profile, args.allow_send, skip_candidates)
                try:
                    round_result = invoke_single_turn(
                        gateway_url,
                        gateway_token,
                        agent_id,
                        prompt,
                        args.request_timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    outcome = fatal_result(
                        "preflight",
                        "gateway_unreachable",
                        profile=profile,
                        agent_id=agent_id,
                        max_rounds=max_rounds,
                        started_at=started_at,
                        results=results,
                    )
                    outcome["fatal_error"]["detail"] = str(exc)
                    append_jsonl(batch_log_path, outcome)
                    return outcome

                results.append(round_result)
                candidate_name = normalize_string(round_result.get("candidate_name"))
                if candidate_name and candidate_name not in skip_seen and round_result["action"] in {"processed", "error"}:
                    skip_seen.add(candidate_name)
                    skip_candidates.append(candidate_name)

                if round_result["action"] == "processed":
                    continue

                if round_result["action"] == "wait_for_unread":
                    ended_at = utc_now()
                    started_ts = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    ended_ts = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                    outcome = {
                        "ok": True,
                        "action": "completed",
                        "profile": profile,
                        "agent_id": agent_id,
                        "gateway_url": gateway_url,
                        "gateway_config_path": str(openclaw_config_path) if openclaw_config_path else "",
                        "max_rounds": max_rounds,
                        "processed_count": sum(1 for item in results if item.get("action") == "processed"),
                        "error_count": sum(1 for item in results if item.get("action") == "error"),
                        "skipped_candidates": skip_candidates,
                        "results": results,
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_seconds": round((ended_ts - started_ts).total_seconds(), 3),
                    }
                    append_jsonl(batch_log_path, outcome)
                    return outcome

                if classify_fatal(round_result):
                    outcome = fatal_result(
                        round_result["error_step"] or "preflight",
                        round_result["error_reason"] or "unknown_fatal_error",
                        profile=profile,
                        agent_id=agent_id,
                        max_rounds=max_rounds,
                        started_at=started_at,
                        results=results,
                    )
                    append_jsonl(batch_log_path, outcome)
                    return outcome

                if not candidate_name:
                    outcome = fatal_result(
                        "preflight",
                        "invalid_json_output",
                        profile=profile,
                        agent_id=agent_id,
                        max_rounds=max_rounds,
                        started_at=started_at,
                        results=results,
                    )
                    append_jsonl(batch_log_path, outcome)
                    return outcome

            ended_at = utc_now()
            started_ts = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            ended_ts = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            outcome = {
                "ok": True,
                "action": "max_rounds_reached",
                "profile": profile,
                "agent_id": agent_id,
                "gateway_url": gateway_url,
                "gateway_config_path": str(openclaw_config_path) if openclaw_config_path else "",
                "max_rounds": max_rounds,
                "processed_count": sum(1 for item in results if item.get("action") == "processed"),
                "error_count": sum(1 for item in results if item.get("action") == "error"),
                "skipped_candidates": skip_candidates,
                "results": results,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": round((ended_ts - started_ts).total_seconds(), 3),
            }
            append_jsonl(batch_log_path, outcome)
            return outcome
    except BlockingIOError:
        outcome = {
            "ok": False,
            "action": "already_running",
            "profile": profile,
            "agent_id": agent_id,
            "max_rounds": max_rounds,
            "processed_count": 0,
            "error_count": 0,
            "results": [],
            "fatal_error": {"step": "preflight", "reason": "lock_conflict"},
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_seconds": 0.0,
        }
        append_jsonl(batch_log_path, outcome)
        return outcome


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    if args.command != "drain-unread":
        raise SystemExit(f"Unsupported command: {args.command}")

    try:
        result = run_drain_unread(args)
    except Exception as exc:  # noqa: BLE001
        failure = {
            "ok": False,
            "action": "fatal_error",
            "fatal_error": {"step": "bootstrap", "reason": str(exc)},
            "started_at": utc_now(),
            "ended_at": utc_now(),
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
