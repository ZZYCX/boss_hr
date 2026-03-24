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
- 输出发送与简历动作链
- 识别最新下载文件

## 主链路

1. `session-check`
2. `plan-next-action`
3. 如果返回 `wait_for_unread`，结束本轮轮询
4. 如果返回 `open_thread`，执行 `data.browser_actions`
5. 重新 `snapshot`
6. 再次运行 `plan-next-action`
7. 如果返回 `llm_reply_required`，直接调用 `data.llm_task_request`
8. 调用 `finalize-reply-plan`
9. 执行返回的 `browser_actions`
10. 执行返回的 `post_actions`
11. 如果需要确认本地落盘结果，可选调用 `resolve-download`

默认无需指定“当前候选人”。
Skill 在会话列表页只检查是否存在未读会话；有未读则选择首个未读会话继续执行，没有未读则返回 `wait_for_unread`。

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
- `llm-task` 结果不符合 schema 时直接失败
- 不在正常流程里做发送前后额外校验
- 不使用消息指纹去重
- `plan-next-action` 只负责选择未读会话、打开会话或产出 `llm_task_request`
- `finalize-reply-plan` 只负责把 `llm-task` 结果变成发送与简历动作链
- 输入框优先用 `#boss-chat-editor-input`
- 发送按钮优先用 `div.submit.active`
- 打开候选人会话优先用页面内 `evaluate` 精确点击外层会话容器
- 会话列表只用于识别未读，不把列表消息摘要当作当前聊天上下文
- 简历下载固定为：同意 -> 预览 -> 下载 -> 关闭预览
- `post_actions` 只负责记录状态
- `resolve-download` 只是下载后的可选确认步骤，不是主流程前置条件

## 参考文件

- `references/workflow.md`
- `references/runtime-contract.md`
- `references/data-contract.md`
- `references/openclaw-browser-relay.md`
- `references/llm-task.md`
- `references/reply-policy.md`
