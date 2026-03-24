# Workflow

## 1. 前置条件

- Boss 网页端已登录
- OpenClaw Browser Relay 可直接操作当前标签页
- 浏览器下载目录可写

## 2. 会话处理

1. `session-check`
2. `plan-next-action`
3. 如果返回 `wait_for_unread`，说明当前没有未读会话，本轮结束
4. 如果返回 `open_thread`，执行返回的 `browser_actions`
5. 重新 snapshot
6. 再次运行 `plan-next-action`
7. 如果返回 `llm_reply_required`，直接调用 `llm-task`
8. 把 `llm-task` JSON 结果交给 `finalize-reply-plan`
9. 执行返回的 `browser_actions`
10. 执行返回的 `post_actions`

## 3. 回复生成

- 本地 Skill 不再生成模板回复文本
- 本地 Skill 不再做消息指纹去重
- 本地 Skill 不再做发送前后冗余校验
- `llm-task` 结果不符合 schema 时直接失败
- 回复生成完全交给 `llm-task`

## 4. 简历处理

1. 如果返回 `reply_and_download_ready`，执行同意/预览/下载/关闭动作链
2. 如需确认文件是否已稳定落地，可选调用 `resolve-download`
3. `resolve-download` 不是回复或下载主流程的前置条件

当前 Skill 在下载完成后结束，不再做后续文件处理。

## 5. 状态记录

- `mark-thread` 只记录状态
- `state_file` 只保留处理痕迹
- 状态数据不参与去重判断
