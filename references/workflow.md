# Boss-HR Workflow

## 1. 前置条件

- 使用已经登录的 Boss 网页端浏览器会话。
- 浏览器动作通过 OpenClaw Browser Relay 执行，而不是本 Skill 自带浏览器驱动。
- 保持下载目录可写，并提前关闭可能拦截下载的浏览器弹窗。
- 在正式执行前先校验 `config/skill-config.toml`。

## 2. 会话接管

1. 通过 OpenClaw Browser Relay 列出已连接标签页，优先复用已登录的 Boss 标签页。
2. 聚焦 Boss 沟通列表页。
3. 确认页面加载完毕后再扫描未读项。
4. 优先使用页面可见文本识别入口：
   - `沟通`
   - `聊天`
   - `未读`
   - 右侧数字红点或消息角标
5. 先把当前 URL 和页面可见文本交给 `scripts/boss_hr.py session-check`。
6. 若页面要求重新登录、验证码或风控校验，立即停止并转人工。

## 3. 未读会话轮询

推荐把主循环固定成下面 6 步：

1. OpenClaw Browser Relay 聚焦 Boss 聊天页并执行一次 `snapshot`。
2. 调用 `scripts/boss_hr.py plan-next-action`。
3. 根据返回的 `action` 决定是否 `act`。
4. 如果执行了浏览器动作，立刻重新 `snapshot`。
5. 如果执行了 `post_actions`，立刻写回 Skill 状态。
6. 当前线程结束后返回列表页，继续下一条未读，直到本轮达到 `max_unread_threads_per_run`。

轮询规则：

- 只取配置中的 `max_unread_threads_per_run` 以内的未读项。
- 每次进入会话后先等待聊天面板和右侧资料面板稳定。
- 每处理完一个会话都返回列表刷新状态，避免重复读取旧角标。
- 每轮循环都先重新 snapshot，再决定是否 act，避免依据过期页面状态连续点击。
- 当 `plan-next-action` 返回 `wait_for_candidates` 时，按 `poll_interval_seconds` 等待后再轮询。

## 4. Snapshot 解析与字段抽取

优先由 `scripts/boss_hr.py plan-next-action` 内部完成 snapshot 解析；只有在排查页面结构变化时，才单独使用 `parse-snapshot`。

每个会话至少抽取以下字段：

- 候选人姓名
- 投递岗位
- 投递时间
- 最近消息列表
- 是否存在简历或附件
- 简历入口文本或附件提示

推荐抽取顺序：

1. 从聊天头部读取 `candidate_name`。
2. 从资料卡、投递卡片或聊天摘要读取 `job_title`。
3. 从投递信息、消息时间或资料面板读取 `delivery_time`。
4. 从消息区自下而上读取最近 1 到 `max_recent_messages` 条消息。
5. 从页面中搜索以下任一文案判断 `has_resume=true`：
   - `已投递简历`
   - `附件`
   - `下载简历`
   - `查看简历`
   - `.pdf`
   - `.doc`
   - `.docx`

## 5. 回复执行顺序

推荐直接使用 `plan-next-action` 的结果来驱动浏览器动作，不再由 OpenClaw 自己拼装回复流程。

1. 调用 `scripts/boss_hr.py plan-next-action`。
2. 若返回 `action=manual_review`，不要发送消息；记录原因并通知用户。
3. 若返回 `action=skip_duplicate`，直接跳过当前会话。
4. 若返回 `action=open_thread`，只执行它给出的点击动作，然后重新 snapshot。
   - 先点会话项外层容器 selector
   - selector 失败后再尝试回退 ref
   - 仍失败时执行 JavaScript 向上查找可点击父节点
   - 点击后必须调用 `verify-thread-open`
5. 若返回 `action=draft_ready`，只读取 `data.reply` 作为草稿，不点击发送。
6. 若返回 `action=reply_ready` 或 `reply_and_download_ready`，按顺序执行：
   - `data.browser_actions` 中的输入动作
   - `data.browser_actions` 中的发送动作
   - `data.post_actions` 中的状态写回动作
7. 在实际执行发送前二次检查：
   - 公司名称是否正确
   - 岗位名称是否正确
   - 问题列表是否与岗位匹配
   - 是否误触发了人工复核场景
8. 发送成功后优先执行 `post_actions` 里的 `mark-thread`，而不是手工拼参数。

输入和发送的执行要求：

- 输入框优先使用 `#boss-chat-editor-input`
- 发送按钮优先使用 `div.submit.active`
- 输入后必须执行动作自带的 `verification`，确认输入框内容已生效
- 点击发送后必须重新 snapshot，并调用 `verify-reply-sent`

## 6. 简历下载顺序

1. 先确认 `plan-next-action` 返回了 `reply_and_download_ready`，或 snapshot 中存在明确的简历入口或附件入口。
2. 点击下载前记录当前下载目录中文件清单。
3. 按 `browser_actions` 触发下载后，循环调用 `scripts/boss_hr.py resolve-download`，直到返回 `download_resolved`。
4. 只在拿到稳定文件路径后，再调用 `scripts/boss_hr.py rename-resume --write-state`。
5. 若返回 `download_unstable`，继续等待，不要过早移动文件。
6. 若下载出错、得到空文件或扩展名异常，停止并转人工。

## 7. 幂等与去重

- 同一会话在同一轮轮询中只处理一次。
- 已发送回复的会话不要再次发送相同消息指纹。
- 已成功归档的相同简历不要再次归档，除非显式 `--force`。
- 线程状态统一写入 `state_file`，不要另建平行账本。
- `plan-next-action` 不直接写状态；只有发送成功后的 `post_actions` 和归档成功后的 `rename-resume --write-state` 才写回账本。

## 8. 停止条件

出现以下任一情况时停止自动化：

- 页面重新登录或触发验证码
- 页面结构明显变化，无法稳定识别姓名、岗位或消息
- 候选人消息涉及薪资、offer、投诉、法律风险
- 简历下载失败或文件损坏
- 消息上下文不足以判断会话阶段

## 9. 建议日志字段

如果需要额外留痕，记录这些字段：

- `candidate_name`
- `job_title`
- `delivery_time`
- `conversation_stage`
- `reply_action`
- `resume_action`
- `timestamp`

## 10. 闭环目标

这份 Skill 在 OpenClaw 中的目标闭环是：

1. 接管已登录 Boss 浏览器标签页。
2. 轮询消息列表并打开待处理会话。
3. 解析会话字段并生成回复计划。
4. 通过 Browser Relay 输入并发送消息。
5. 检测简历入口、触发下载、解析下载结果。
6. 将简历重命名为 `投递岗位_名字_投递时间` 并归档。
