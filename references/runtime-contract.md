# Runtime Contract

## 主链路

当前主链路固定为：

1. `plan-next-action`
2. `llm-task`
3. `finalize-reply-plan`

## 支持命令

- `validate-config`
- `session-check`
- `parse-snapshot`
- `plan-next-action`
- `finalize-reply-plan`
- `resolve-download`
- `mark-thread`
- `show-state`

## JSON 包装格式

所有命令统一返回：

```json
{
  "ok": true,
  "command": "plan-next-action",
  "action": "llm_reply_required",
  "data": {},
  "errors": [],
  "timestamp": "2026-03-20T10:00:00Z"
}
```

## `plan-next-action`

可能返回：

- `wait_for_candidates`
- `open_thread`
- `thread_not_found`
- `llm_reply_required`
- `unknown_page`
- `session_ok` 之外的会话异常动作

### `open_thread`

`data.browser_actions` 是浏览器执行动作。优先按返回的 `selector` / `selectors` 点击，不要在外层自己重写点击逻辑。

### `llm_reply_required`

`data` 中会包含：

- `thread_id`
- `candidate`
- `job_family`
- `llm_task_request`
- `next_command=finalize-reply-plan`

Runner 必须直接调用 `llm_task_request`，不要自己重写 prompt。

## `finalize-reply-plan`

输入：

- 当前线程 snapshot
- 可选 candidate override
- `llm-task` 返回的结构化 JSON

可能返回：

- `draft_ready`
- `reply_ready`
- `reply_and_download_ready`

返回字段：

- `thread_id`
- `reply`
- `stage`
- `job_family`
- `browser_actions`
- `post_actions`

## Browser Actions

回复发送阶段只保留必要动作，不再附带发送前后冗余校验。

典型结构：

```json
{
  "executor": "browser",
  "kind": "type",
  "selector": "#boss-chat-editor-input",
  "text": "..."
}
```

```json
{
  "executor": "browser",
  "kind": "click",
  "selector": "div.submit.active"
}
```

规则：

- 输入框优先使用 `#boss-chat-editor-input`
- 发送按钮优先使用 `div.submit.active`
- 按 `browser_actions` 顺序执行
- `post_actions` 只在浏览器动作成功后执行

## 状态与日志

当前状态文件只用于记录会话处理结果，不参与去重判断。

- 不再基于消息指纹跳过回复
- 不再基于线程状态阻止重复发送
- `mark-thread` 只记录状态
