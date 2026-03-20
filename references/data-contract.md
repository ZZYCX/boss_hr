# Data Contract

## 1. 候选人上下文 JSON

`scripts/render_reply.py` 接收的候选人上下文应尽量满足以下结构：

```json
{
  "thread_id": "thread_123",
  "candidate_name": "张三",
  "job_title": "Java后端工程师",
  "delivery_time": "2026-03-17 14:30",
  "company_name": "示例公司",
  "conversation_stage": "after_intro",
  "has_resume": true,
  "resume_hint": "已投递简历",
  "recent_messages": [
    {
      "role": "candidate",
      "text": "您好，我有 5 年 Java 后端经验。",
      "timestamp": "2026-03-17 14:31"
    }
  ]
}
```

## 2. 必填字段

- `candidate_name`
- `job_title`
- `recent_messages`

`delivery_time` 强烈建议提供；若缺失，简历重命名阶段需要手动补齐。
`thread_id` 强烈建议提供；若缺失，统一入口脚本会退化为基于姓名、岗位、投递时间生成的派生键。

## 3. recent_messages 约定

- `role` 允许值：`candidate`、`hr`、`system`
- `text` 为消息正文
- `timestamp` 为页面可见时间字符串；若拿不到可以省略
- 顺序按页面阅读顺序保留，脚本会自动读取最后一条作为 `latest_message`

## 4. parse-snapshot 输出结构

当 OpenClaw Browser Relay 提供 snapshot 文本时，`scripts/boss_hr.py parse-snapshot` 会返回：

```json
{
  "page_kind": "thread_view",
  "current_url": "https://www.zhipin.com/web/geek/chat",
  "visible_text": "张子毅\n职位：数据标注实习生\n发送\n下载简历",
  "thread_refs": [
    {
      "ref": "e21",
      "label": "张子毅",
      "kind": "link"
    }
  ],
  "candidate": {
    "thread_id": "",
    "candidate_name": "张子毅",
    "job_title": "数据标注实习生",
    "delivery_time": "",
    "company_name": "广州浩传网络科技有限公司",
    "has_resume": true,
    "resume_hint": "下载简历",
    "recent_messages": [
      {
        "role": "candidate",
        "text": "需要了解目前后端相关经验年限、熟悉的技术栈、到岗时间"
      }
    ]
  },
  "reply_input": {
    "refs": [{"ref": "e30", "label": "请输入消息"}],
    "selectors": ["#boss-chat-editor-input", "textarea", "[contenteditable='true']"]
  },
  "send_button": {
    "refs": [{"ref": "e31", "label": "发送"}],
    "selectors": ["div.submit.active", "button[type='submit']"]
  },
  "resume_targets": [
    {
      "ref": "e32",
      "label": "下载简历",
      "kind": "link",
      "selectors": ["a[href*='resume']", ".resume-item a", ".btn-download"]
    }
  ]
}
```

`page_kind` 当前支持：

- `chat_empty`
- `chat_list`
- `thread_view`
- `unknown`

## 5. plan-next-action 输出结构

OpenClaw 自动化主流程应优先消费 `plan-next-action` 的结果：

```json
{
  "action": "reply_and_download_ready",
  "data": {
    "thread_id": "thread_123",
    "reply": "你好，我是广州浩传网络科技有限公司的HR...",
    "browser_actions": [
      {
        "executor": "browser",
        "kind": "type",
        "ref": "e30",
        "selector": "#boss-chat-editor-input",
        "text": "..."
      },
      {
        "executor": "browser",
        "kind": "click",
        "ref": "e31",
        "selector": "div.submit.active"
      },
      {
        "executor": "browser",
        "kind": "click",
        "ref": "e32",
        "selector": "a[href*='resume']"
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

契约要求：

- `browser_actions` 按数组顺序执行。
- `post_actions` 只在浏览器动作成功后执行。
- `reply` 始终以最终发送文本出现，不需要 OpenClaw 再次生成。

## 6. 简历命名规则

目标格式：

```text
投递岗位_姓名_投递时间.ext
```

示例：

```text
Java后端工程师_张三_2026-03-17-1430.pdf
```

## 7. 文件名清洗规则

- 去除 Windows 非法字符：`/ \ : * ? " < > |`
- 所有连续空白替换为单个下划线 `_`
- 去掉首尾下划线和句点
- 保留原始扩展名

## 8. 时间规范

统一输出格式：

```text
YYYY-MM-DD-HHmm
```

`scripts/rename_resume.py` 支持以下常见输入格式：

- `2026-03-17 14:30`
- `2026/03/17 14:30`
- `2026-03-17`
- `2026/03/17`
- `2026年03月17日 14:30`
- `2026-03-17-1430`

## 9. 下载解析结果

`scripts/boss_hr.py resolve-download` 返回的稳定文件对象结构：

```json
{
  "action": "download_resolved",
  "download_root": "C:/BossHR/downloads",
  "file": {
    "path": "C:/BossHR/downloads/Java后端工程师_张三.pdf",
    "name": "Java后端工程师_张三.pdf",
    "size_bytes": 123456,
    "modified_epoch": 1773818684.0
  }
}
```

OpenClaw 应把 `file.path` 直接作为 `rename-resume --source` 的输入。

## 10. 日志建议

若需要记录处理结果，可复用以下字段：

```json
{
  "candidate_name": "张三",
  "job_title": "Java后端工程师",
  "reply_action": "reply",
  "resume_action": "renamed",
  "resume_path": "C:/BossHR/resumes/Java后端工程师_张三_2026-03-17-1430.pdf"
}
```
