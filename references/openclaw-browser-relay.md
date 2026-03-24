# OpenClaw Browser Relay

## 目标

把 Browser Relay 固定为页面执行层，把 `boss_hr.py + llm-task` 固定为决策层。

## 浏览器层职责

- 找到已登录的 Boss 标签页
- 采集 `snapshot`
- 执行 `browser_actions`
- 触发简历下载

## Skill 层职责

- 解析 snapshot
- 产出 `llm_task_request`
- 消费 `llm-task` 结构化结果
- 产出输入/发送动作
- 记录线程状态
- 解析下载结果

## 推荐链路

1. `snapshot`
2. `session-check`
3. `plan-next-action`
4. 如果返回 `open_thread`，执行会话打开动作
5. `snapshot`
6. `plan-next-action`
7. 如果返回 `llm_reply_required`，调用 `llm-task`
8. `finalize-reply-plan`
9. 执行 `browser_actions`
10. 如有需要，调用 `resolve-download`

## 执行规则

- 不要绕过 `llm-task`
- 不要自行重写 `llm_task_request`
- 不要额外插入发送前后校验命令
- 下载触发后，Skill 只负责识别最新稳定文件
- 输入框优先用 `#boss-chat-editor-input`
- 发送按钮优先用 `div.submit.active`
