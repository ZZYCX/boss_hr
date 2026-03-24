# Workflow

## 1. 前置条件

- Boss 网页端已登录
- OpenClaw Browser Relay 可直接操作当前标签页
- 浏览器下载目录可写

## 2. 会话处理

1. `session-check`
2. `plan-next-action`
3. 如果返回 `open_thread`，执行返回的 `browser_actions`
4. 重新 snapshot
5. 再次运行 `plan-next-action`
6. 如果返回 `llm_reply_required`，直接调用 `llm-task`
7. 把 `llm-task` JSON 结果交给 `finalize-reply-plan`
8. 执行返回的 `browser_actions`
9. 执行返回的 `post_actions`

## 3. 回复生成

- 本地 Skill 不再生成模板回复文本
- 本地 Skill 不再做消息指纹去重
- 本地 Skill 不再做发送前后冗余校验
- 回复生成完全交给 `llm-task`

## 4. 简历处理

1. 如果返回 `reply_and_download_ready`，执行下载动作
2. 如需确认文件是否已稳定落地，调用 `resolve-download`
3. 到 `download_resolved` 为止

当前 Skill 在下载完成后结束，不再做后续文件处理。

## 5. 状态记录

- `mark-thread` 只记录状态
- `state_file` 只保留处理痕迹
- 状态数据不参与去重判断
