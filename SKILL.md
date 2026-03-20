---
name: boss-hr-assistant
description: 接管已登录的 Boss 直聘网页端浏览器会话，轮询未读候选人消息，提取候选人姓名、投递岗位、投递时间和最近消息，根据岗位类型和消息上下文生成 HR 回复，并识别“已投递简历”“有附件”或“可下载简历”的会话后自动下载、重命名和归档简历。用于处理 Boss 网页端招聘沟通、批量清理未读消息、统一 HR 初筛话术、下载候选人简历或整理简历文件时。
---

# Boss HR Assistant

接管已登录的 Boss 网页端浏览器会话，按工作流处理未读候选人消息。这个 Skill 预期安装在 OpenClaw 的 `skills/` 目录中，由 OpenClaw Browser Relay 执行浏览器动作；业务规则、状态去重和文件归档交给统一入口脚本。

**Primary Script:** `{baseDir}/scripts/boss_hr.py`
**Config:** `{baseDir}/config/skill-config.toml`
**Workflow Reference:** `{baseDir}/references/workflow.md`
**Reply Policy:** `{baseDir}/references/reply-policy.md`
**Data Contract:** `{baseDir}/references/data-contract.md`
**Runtime Contract:** `{baseDir}/references/runtime-contract.md`
**OpenClaw Relay Reference:** `{baseDir}/references/openclaw-browser-relay.md`

## Preconditions

- 保持 Boss 网页端处于已登录状态。
- 通过 OpenClaw Browser Relay 连接真实浏览器标签页，不要新建未登录会话。
- 保证下载目录、归档目录和状态目录都有写权限。
- 明确当前是否允许自动发送；若用户只要求起草，不要点击发送。
- 遇到薪资、offer、投诉、风控、验证码、法律风险内容时转人工。

## OpenClaw Execution

- 用 OpenClaw Browser Relay 负责 `tabs`、`snapshot`、`act` 这类浏览器动作。
- 用 `scripts/boss_hr.py` 负责判断、去重、落日志和归档。
- 每次 act 前都基于最新 snapshot 重新判断，不要连续盲点。
- 优先复用 Browser Relay 的 `chrome-relay` 已登录标签页；必要时再考虑 `user` 作为后备。

## Command Workflow

### 1. Validate Config

```bash
python scripts/boss_hr.py --config config/skill-config.toml validate-config
```

先修复任何配置错误，再继续执行。

### 2. Check Session

让 OpenClaw Browser Relay 提供当前 URL 和页面可见文本，然后运行：

```bash
python scripts/boss_hr.py --config config/skill-config.toml session-check --current-url "https://www.zhipin.com/web/geek/chat" --page-file boss-page.txt --write-state
```

只在返回 `action=session_ok` 时继续。若返回：

- `login_required`
  - 让用户手工重新登录。
- `risk_control`
  - 停止自动化，不要继续点击。
- `wrong_page`
  - 先切回 Boss 沟通工作页。

### 3. Parse Snapshot

需要单独调试 snapshot 解析时运行：

```bash
python scripts/boss_hr.py --config config/skill-config.toml parse-snapshot --current-url "https://www.zhipin.com/web/geek/chat" --snapshot-file boss-snapshot.txt
```

这个命令会返回：

- `page_kind`
- `thread_refs`
- `candidate`
- `reply_input`
- `send_button`
- `resume_targets`

正常自动化主流程优先使用下一步的 `plan-next-action`，只有在排查页面结构变化时才单独看 `parse-snapshot`。

### 4. Plan Next Action

OpenClaw Browser Relay 每次拿到最新 snapshot 后，优先调用：

```bash
python scripts/boss_hr.py --config config/skill-config.toml plan-next-action --current-url "https://www.zhipin.com/web/geek/chat" --snapshot-file boss-snapshot.txt --allow-send --include-resume
```

统一入口脚本会同时完成：

- 会话有效性判断
- snapshot 解析
- 候选人字段提取
- 岗位家族匹配
- 会话阶段判断
- 人工复核拦截
- 基于状态文件的去重
- 生成 Browser Relay 下一步动作计划

常见返回动作：

- `wait_for_candidates`
  - 当前列表为空，等待下次轮询。
- `open_thread`
  - 先点击某个候选人会话；这一步现在会优先点对话项外层可点击容器，而不是名字文字节点。
- `draft_ready`
  - 生成了回复草稿，但当前不允许自动发送。
- `reply_ready`
  - 生成了发送计划。
- `reply_and_download_ready`
  - 生成了发送计划，同时识别到简历下载入口。
- `manual_review`
  - 命中人工复核边界，停止自动化。
- `skip_duplicate`
  - 当前线程已经处理过同一消息指纹。

OpenClaw 应严格按 `data.browser_actions` 顺序执行 Browser Relay `act`，完成后再按 `data.post_actions` 调用 Skill 命令。
如果某个 `browser_action` 带有 `verification`，则该动作完成后必须立刻重新 snapshot 并执行对应校验，不通过时不要继续下一个动作。

### 5. Draft Reply

如果浏览器层已经有稳定的候选人 JSON，不想依赖 snapshot 解析，也可以直接运行：

```bash
python scripts/boss_hr.py --config config/skill-config.toml draft-reply --candidate-file candidate.json --thread-id thread_123 --write-state
```

统一入口脚本会同时完成：

- 岗位家族匹配
- 会话阶段判断
- 人工复核拦截
- 基于状态文件的去重
- JSON 输出

只在返回 `action=reply` 时发送。若返回：

- `manual_review`
  - 停止发送并把原因反馈给用户。
- `skip_duplicate`
  - 当前线程的相同消息指纹已经处理过，不要重复发送。

### 6. Mark Manual Steps

OpenClaw Browser Relay 实际发送成功或需要人工标记时，写入线程状态：

```bash
python scripts/boss_hr.py --config config/skill-config.toml mark-thread --thread-id thread_123 --status replied --candidate-name "张三" --job-title "Java后端工程师"
```

常用状态：

- `seen`
- `drafted`
- `replied`
- `manual_review`
- `resume_downloaded`

### 7. Resolve Download and Archive Resume

Browser Relay 触发简历下载后，先解析下载目录中的最新稳定文件：

```bash
python scripts/boss_hr.py --config config/skill-config.toml resolve-download --download-root "C:\Users\hc\Downloads"
```

只在返回 `action=download_resolved` 时继续归档。若返回：

- `download_unstable`
  - 文件还在写入中，稍后重试。
- `no_download_found`
  - 下载未成功触发，重新 snapshot 并检查页面。
- `download_root_missing`
  - 配置或本地目录错误。

随后执行：

当会话存在“已投递简历 / 有附件 / 下载简历 / 查看简历”入口且下载完成后，运行：

```bash
python scripts/boss_hr.py --config config/skill-config.toml rename-resume --source "C:\Users\hc\Downloads\resume.pdf" --job-title "Java后端工程师" --candidate-name "张三" --delivery-time "2026-03-17 14:30" --thread-id thread_123 --write-state
```

统一入口脚本会：

- 计算目标文件名
- 做 Windows 文件名清洗
- 按规则归档
- 防止同一线程重复归档相同简历
- 追加 JSONL 日志

### 8. Inspect State

```bash
python scripts/boss_hr.py --config config/skill-config.toml show-state
python scripts/boss_hr.py --config config/skill-config.toml show-state --thread-id thread_123
```

## Required Files

- `scripts/boss_hr.py`
  - 统一入口脚本；优先使用它而不是直接拼接多个子脚本。
- `scripts/parse_boss_snapshot.py`
  - Browser Relay snapshot 解析模块。
- `scripts/resolve_download.py`
  - 下载目录稳定文件解析模块。
- `scripts/validate_config.py`
  - 配置校验模块。
- `scripts/render_reply.py`
  - 回复生成模块。
- `scripts/rename_resume.py`
  - 简历重命名模块。
- `references/runtime-contract.md`
  - JSON 输出契约、状态文件结构和去重规则。
- `references/openclaw-browser-relay.md`
  - OpenClaw Browser Relay 的分层职责、推荐动作序列和注意事项。
- `references/workflow.md`
  - 浏览器层工作流和停止条件。
- `references/reply-policy.md`
  - 人工复核边界和岗位模板策略。
- `references/data-contract.md`
  - 候选人上下文 JSON 和文件命名规范。

## Operating Rules

- 优先使用页面可见文案，不要先写死脆弱 CSS 选择器。
- OpenClaw Browser Relay 负责“采集”和“点击”；统一入口脚本负责“判断”和“记账”。
- 任何自动发送前都先读取统一入口脚本的 JSON 结果。
- 打开候选人会话时，优先点击对话项外层容器 selector；原始文字节点 ref 只作为回退。
- 聊天输入框必须优先使用 `#boss-chat-editor-input`。
- 发送按钮必须优先使用 `div.submit.active`。
- OpenClaw 每次执行 `browser_actions` 后都要重新 snapshot，不要基于旧页面继续盲点。
- 状态文件用于避免重复回复、重复归档和重复人工处理。
- 不要承诺面试、薪资、offer、入职日期。

## Output Rules

- 所有统一入口命令都输出单个 JSON 对象。
- 回复输出必须落在 `data.reply` 中。
- 浏览器动作计划必须落在 `data.browser_actions` 中。
- 发送成功后的状态写回计划必须落在 `data.post_actions` 中。
- 简历归档结果必须落在 `data.target_path` 中。
- 所有时间统一格式为 `YYYY-MM-DD-HHmm`。
- 字段缺失时停止并返回错误，不要伪造姓名、岗位或投递时间。

## Minimal Example

候选人上下文文件示例：

```json
{
  "candidate_name": "张三",
  "job_title": "Java后端工程师",
  "delivery_time": "2026-03-17 14:30",
  "recent_messages": [
    {
      "role": "candidate",
      "text": "您好，我有 5 年 Java 后端经验，最近在看新机会。",
      "timestamp": "2026-03-17 14:31"
    }
  ],
  "has_resume": true,
  "resume_hint": "已投递简历"
}
```

按 snapshot 自动规划下一步：

```bash
python scripts/boss_hr.py --config config/skill-config.toml plan-next-action --current-url "https://www.zhipin.com/web/geek/chat" --snapshot-file boss-snapshot.txt --allow-send --include-resume
```

直接起草回复：

```bash
python scripts/boss_hr.py --config config/skill-config.toml draft-reply --candidate-file candidate.json --thread-id thread_123 --write-state
```

检查会话：

```bash
python scripts/boss_hr.py --config config/skill-config.toml session-check --current-url "https://www.zhipin.com/web/geek/chat" --page-file boss-page.txt --write-state
```
