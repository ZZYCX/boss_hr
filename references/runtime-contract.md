# Runtime Contract

## Primary Script

Use `scripts/boss_hr.py` as the single entry point. Keep `render_reply.py`, `rename_resume.py`, and `validate_config.py` as focused helper modules behind it.

## Supported Commands

- `validate-config`
  - Validate `config/skill-config.toml` and return a JSON summary.
- `session-check`
  - Inspect current Boss page URL and visible text.
  - Decide whether the session is usable, expired, blocked by risk control, or on the wrong page.
- `parse-snapshot`
  - Parse Browser Relay snapshot text into candidate fields, thread refs, input refs, send refs, and resume refs.
- `verify-thread-open`
  - Verify that the clicked thread is actually the expected candidate thread.
- `plan-next-action`
  - Parse the current snapshot, infer page kind, draft reply content, and emit the next Browser Relay action plan.
- `draft-reply`
  - Read candidate context JSON.
  - Apply岗位模板、人工复核规则、去重策略。
  - Return either `reply`, `manual_review`, or `skip_duplicate`.
- `verify-reply-sent`
  - Verify that the reply text is visible in the expected candidate thread after clicking send.
- `resolve-download`
  - Inspect the download directory and return the newest stable resume file.
- `rename-resume`
  - Rename and archive a downloaded resume.
  - Prevent duplicate archiving for the same thread and delivery time unless `--force` is used.
- `mark-thread`
  - Update thread state after manual actions such as `seen`, `replied`, `manual_review`, `resume_downloaded`.
- `show-state`
  - Read the persisted runtime state.

## JSON Envelope

Every command should return a single JSON object:

```json
{
  "ok": true,
  "command": "draft-reply",
  "action": "reply",
  "data": {},
  "errors": [],
  "timestamp": "2026-03-17T12:00:00Z"
}
```

Rules:

- `ok`
  - `true` when the command completed normally, even if the action is `manual_review` or `skip_duplicate`.
- `command`
  - The invoked subcommand name.
- `action`
  - The final machine-readable result.
- `data`
  - Command-specific payload.
- `errors`
  - Non-empty only when `ok=false`.
- `timestamp`
  - UTC ISO8601 string.

## Session Actions

- `session_ok`
  - 当前 Boss 会话仍然可用；判断依据是 Boss 域名、聊天页 URL，且未命中登录或风控文本。
- `login_required`
  - 页面提示登录，或 URL 明显不在 Boss 聊天页。
- `risk_control`
  - 页面出现验证码、风控、异常访问等阻断文案。
- `wrong_page`
  - 域名正确，但当前页不是聊天工作页。

`ready_hits` 仍会保留在输出里，但只作为辅助信号，不再是 `session_ok` 的必要条件。

## Planner Actions

`plan-next-action` 可能返回这些动作：

- `wait_for_candidates`
  - 当前聊天列表为空，OpenClaw 应等待下一轮轮询。
- `open_thread`
  - 先执行 `data.browser_actions` 中的点击动作，打开某个候选人会话。
- `thread_not_found`
  - 指定候选人不在当前会话列表中，OpenClaw 不应误点其他人。
- `candidate_incomplete`
  - snapshot 中缺少姓名、岗位或最近消息，不能安全生成回复。
- `manual_review`
  - 命中人工复核边界，停止自动化。
- `skip_duplicate`
  - 当前线程已处理过同一消息指纹。
- `draft_ready`
  - 已生成回复草稿，但当前不允许自动发送。
- `reply_ready`
  - 已生成输入和发送动作计划。
- `reply_and_download_ready`
  - 已生成输入、发送和简历下载动作计划。
- `unknown_page`
  - snapshot 不能稳定分类，OpenClaw 应重新 snapshot 或转人工。

## Browser Action Payload

当动作是 `open_thread`、`reply_ready` 或 `reply_and_download_ready` 时，`data.browser_actions` 为有序数组。每一项都遵循下面结构：

```json
{
  "executor": "browser",
  "kind": "click",
  "description": "点击发送按钮",
  "ref": "e31",
  "selector": "div.submit.active",
  "selectors": ["div.submit.active", "button[type='submit']"],
  "label": "发送"
}
```

规则：

- 对 `thread_item`，优先使用外层容器 `selector` / `selectors`，不要先点文字节点 `ref`。
- 对 `chat_input`，优先使用 `#boss-chat-editor-input`。
- 对 `send_button`，优先使用 `div.submit.active`。
- 当首选 selector 失败时，才回退到 `fallback_refs` 或 `fallback_clicks`。
- OpenClaw 必须按数组顺序执行，不要自行重排。

`thread_item` 动作还会附带：

- `fallback_clicks`
  - 先 selector 容器
  - 再 raw ref
  - 最后 JavaScript 向上找可点击父节点
- `verification`
  - 点击后必须重新 snapshot，并调用 `verify-thread-open`

`chat_input` 和 `send_button` 动作也会附带 `verification`：

- `chat_input`
  - 浏览器层应执行 JavaScript 校验，确认输入框里已出现预期文本
- `send_button`
  - 浏览器层应执行 JavaScript 初检
  - 然后重新 snapshot 并调用 `verify-reply-sent`

## Post Action Payload

发送成功后执行 `data.post_actions`。当前主要是 `mark-thread`：

```json
{
  "executor": "skill",
  "kind": "mark-thread",
  "args": {
    "thread_id": "thread_123",
    "status": "replied",
    "candidate_name": "张三",
    "job_title": "Java后端工程师"
  }
}
```

只有在浏览器动作确认成功后才执行 `post_actions`，不要提前记账。

## Download Resolution Actions

`resolve-download` 可能返回：

- `download_resolved`
  - 已找到最新稳定文件，可以继续 `rename-resume`。
- `download_unstable`
  - 文件还在增长，稍后重试。
- `no_download_found`
  - 尚未发现新文件。
- `download_root_missing`
  - 下载目录不存在或配置错误。

## Verification Actions

`verify-thread-open` 可能返回：

- `thread_verified`
  - 已进入目标候选人会话。
- `still_in_list`
  - 点击后仍停留在列表页，应尝试下一个回退点击方案。
- `thread_mismatch`
  - 已进入错误候选人会话，必须停止继续自动发送。
- `thread_open_uncertain`
  - 页面状态不足以确认，重新 snapshot 或转人工。

`verify-reply-sent` 可能返回：

- `reply_sent`
  - 发送后的 snapshot 已能看到回复内容。
- `reply_send_unverified`
  - 仍在正确会话，但回复内容尚未可靠出现，应重新 snapshot 再判断。
- `thread_mismatch`
  - 发送后发现当前会话不是目标候选人。
- `reply_send_uncertain`
  - 页面状态不足以确认发送结果。

## Thread State Model

State file is a single JSON document managed by the primary script:

```json
{
  "version": 1,
  "session": {
    "last_action": "session_ok",
    "checked_at": "2026-03-17T12:00:00Z"
  },
  "threads": {
    "thread_123": {
      "candidate_name": "张三",
      "job_title": "Java后端工程师",
      "delivery_time": "2026-03-17 14:30",
      "last_message_fingerprint": "sha256...",
      "last_reply_action": "reply",
      "last_reply_at": "2026-03-17T12:05:00Z",
      "last_reply_text": "收到，感谢你的介绍...",
      "resume_keys": ["sha256..."],
      "statuses": ["seen", "replied"]
    }
  }
}
```

## Dedupe Rules

- Reply dedupe key:
  - `thread_id` if available.
  - Otherwise `candidate_name + job_title + delivery_time`.
- Reply fingerprint:
  - Hash `conversation_stage + recent_messages`.
- Skip duplicate replies when:
  - the same thread already recorded the same fingerprint, and
  - the stored action is `reply` or `manual_review`.
- Resume dedupe key:
  - `job_title + candidate_name + delivery_time + extension`.

## Logging

When `draft-reply` or `rename-resume` writes state, also append one JSON line to the corresponding log file:

- `reply_log_file`
- `resume_log_file`

Each JSON line should be self-contained and include:

- `thread_id`
- `candidate_name`
- `job_title`
- `action`
- `timestamp`

## Browser Boundary

This Skill is designed for OpenClaw Browser Relay. The browser layer should gather page URL, visible text, and stable refs through OpenClaw `tabs` / `snapshot` / `act`, then hand those artifacts to `boss_hr.py`. Business logic stays inside the skill; Browser Relay only executes the returned plan.
