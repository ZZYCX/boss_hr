# LLM Task

## 目标

`boss-hr-assistant` 不直接调用外部模型 API，统一通过 OpenClaw 的 `llm-task` 生成结构化回复结果。

## 调用方式

优先直接消费 `plan-next-action` 返回的：

- `data.llm_task_request.tool`
- `data.llm_task_request.args`

其中 `args` 至少包含：

- `prompt`
- `input`
- `schema`
- `thinking`

## 输出格式

`llm-task` 必须返回 JSON：

```json
{
  "reply_text": "你好，想先和你确认一下目前所在城市和到岗时间。",
  "conversation_stage": "after_intro"
}
```

拿到结果后，立即调用 `finalize-reply-plan`。
