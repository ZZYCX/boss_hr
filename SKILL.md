---
name: boss-hr-assistant
description: 接管已登录的 Boss 网页端会话，解析候选人聊天上下文，通过 OpenClaw 的 llm-task 生成结构化回复结果，再生成发送动作并处理简历下载。适用于 Boss 招聘沟通自动回复、未读会话处理和简历下载。
---

# Boss HR Assistant

这个 Skill 运行在 OpenClaw + Browser Relay 环境里。

浏览器层职责：
- 复用已登录的 Boss 标签页
- 执行 `snapshot`
- 按 Skill 返回的 `browser_actions` 点击、输入、发送
- 触发简历下载

Skill 职责：
- 解析 snapshot
- 生成 `llm-task` 请求
- 消费 `llm-task` 结构化结果
- 输出发送动作
- 识别最新下载文件

## 主链路

1. `session-check`
2. `plan-next-action`
3. 如果返回 `open_thread`，执行 `data.browser_actions`
4. 重新 `snapshot`
5. 再次运行 `plan-next-action`
6. 如果返回 `llm_reply_required`，直接调用 `data.llm_task_request`
7. 调用 `finalize-reply-plan`
8. 执行返回的 `browser_actions`
9. 执行返回的 `post_actions`
10. 如果有简历下载，只走 `resolve-download`

## 命令

优先使用：

```bash
python scripts/boss_hr.py --config config/skill-config.toml plan-next-action ...
python scripts/boss_hr.py --config config/skill-config.toml finalize-reply-plan ...
python scripts/boss_hr.py --config config/skill-config.toml resolve-download ...
python scripts/boss_hr.py --config config/skill-config.toml mark-thread ...
```

## 关键规则

- 回复内容统一交给 OpenClaw `llm-task`
- 回复不再由本地模板脚本生成
- 不在正常流程里做发送前后额外校验
- 不使用消息指纹去重
- `plan-next-action` 只负责打开会话或产出 `llm_task_request`
- `finalize-reply-plan` 只负责把 `llm-task` 结果变成发送动作
- 输入框优先用 `#boss-chat-editor-input`
- 发送按钮优先用 `div.submit.active`
- `post_actions` 只负责记录状态
- 简历只下载，不做后续文件处理

## 参考文件

- `references/workflow.md`
- `references/runtime-contract.md`
- `references/data-contract.md`
- `references/openclaw-browser-relay.md`
- `references/llm-task.md`
- `references/reply-policy.md`
