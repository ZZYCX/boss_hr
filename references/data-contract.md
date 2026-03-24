# Data Contract

## 1. 候选人上下文

`plan-next-action` 在会话页会整理出候选人上下文，典型结构：

```json
{
  "thread_id": "thread_123",
  "candidate_name": "张三",
  "job_title": "Java后端工程师",
  "delivery_time": "2026-03-17 14:30",
  "company_name": "广州浩传网络科技有限公司",
  "has_resume": true,
  "resume_hint": "已投递简历",
  "recent_messages": [
    {
      "role": "candidate",
      "text": "您好，我有 5 年 Java 后端经验，目前在职。"
    }
  ]
}
```

## 2. `llm_task_request`

当 `plan-next-action` 返回 `llm_reply_required` 时，`data` 中至少包含：

```json
{
  "thread_id": "thread_123",
  "job_family": "java-backend",
  "candidate": {},
  "llm_task_request": {
    "tool": "llm-task",
    "args": {
      "prompt": "...",
      "input": {},
      "schema": {},
      "thinking": "low"
    }
  }
}
```

OpenClaw 必须直接使用这里返回的 `tool` 和 `args` 调用 `llm-task`。

## 3. `llm-task` 结果

`llm-task` 输出必须是 JSON，对应字段：

```json
{
  "reply_text": "你好，想先和你确认一下目前所在城市、后端相关经验年限和到岗时间。",
  "conversation_stage": "after_intro"
}
```

## 4. `finalize-reply-plan`

当 `llm-task` 结果可自动发送时，`finalize-reply-plan` 会返回：

```json
{
  "action": "reply_and_download_ready",
  "data": {
    "thread_id": "thread_123",
    "reply": "你好，想先和你确认一下目前所在城市、后端相关经验年限和到岗时间。",
    "browser_actions": [
      {
        "executor": "browser",
        "kind": "type",
        "selector": "#boss-chat-editor-input",
        "text": "..."
      },
      {
        "executor": "browser",
        "kind": "click",
        "selector": "div.submit.active"
      }
    ],
    "post_actions": [
      {
        "executor": "skill",
        "kind": "mark-thread",
        "args": {
          "thread_id": "thread_123",
          "status": "replied"
        }
      }
    ]
  }
}
```

## 5. 下载解析结果

`resolve-download` 返回：

```json
{
  "action": "download_resolved",
  "download_root": "/data/openclaw/skills/boss-hr-assistant/runtime/downloads",
  "file": {
    "path": "/data/openclaw/skills/boss-hr-assistant/runtime/downloads/resume.pdf",
    "name": "resume.pdf",
    "size_bytes": 123456,
    "modified_epoch": 1773818684.0
  }
}
```

当前 Skill 到识别最新稳定下载文件为止。
