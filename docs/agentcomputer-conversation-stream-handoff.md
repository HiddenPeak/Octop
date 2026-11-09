# App 智能体与任务共用正文 WebSocket：客户端接入交接

## 智能体从缓存续接

首发服务端在 `GET /api/agents/resident-capabilities` 声明 `conversation_cursor_sync: true` 和 `conversation_stream: {transport:"websocket", path:"/api/project-tasks/ws", protocol_version:3}`。App 与盒子网页都使用这套时间线。服务端要求 `resident.subscribe` 带 `timeline:true`。

```json
{"type":"resident.subscribe","id":"resident-first-0001","agent_id":"resident_agent_example","thread_id":"resident_thread_example","timeline":true}
```

首次订阅或重建时，服务端先发 `resident.timeline.snapshot`，再补齐期间的变化并发出 `result`。快照 `data` 包含 `agent_id`、`thread_id`、`revision`、`cursor`、`active_turn_id`、`settings`、`inbox`、`items` 和 `next_cursor`。`items` 是最新最多 80 个时间线条目，与 `GET .../conversation/history` 使用同一结构，包含 `turn`、`task_report` 和已投递的 `notification`。`next_cursor` 交给历史接口翻更早的页。

```json
{"type":"resident.subscribe","id":"resident-resume-0001","agent_id":"resident_agent_example","thread_id":"resident_thread_example","timeline":true,"cursor":"opaque_resident_cursor"}
```

带 `cursor` 时，服务端先发 `resident.timeline.update` 补齐该位置之后的变化，再发 `result`。没有任何变化时只有 `result`。后续实时推送也使用 `resident.timeline.update`，字段与快照相同，历史分页的 `next_cursor` 仅在快照中返回。每条补发内容都是该项最新的完整状态。仅已读、配置等会话状态变化时，`items` 可以为空。

- 重连前登记事件接收方。应用快照或按 `item_id` 合并全部补发项后，将已应用的 `cursor` 和缓存一起写入。打开时可以用同一订阅的 `result` 作为追平后发布的界面边界。收到正文更新即可刷新当前轮，不需要再等历史首屏。历史页按 `history_revision` 处理迟到的响应，永远不推进实时 `cursor`。
- 用 `created_at` 和稳定 `item_id` 排序，用 `day_key` 分组。离线期间更新的旧回报、晚投递的通知可能插入较早位置。轮次按 `turn_id` 和单轮 `revision` 合并完整正文，状态变化也应用。已读位置继续通过 `POST .../conversation/read` 提交实际读到的 `message_id`。
- 游标绑定盒子、账号和会话，不能用 Task 游标、历史分页游标或裸 `revision` 替代。游标无效或离线积累超过 80 个变化项时，服务端返回 `PROJECT_SYNC_RESET`。客户端用 `timeline:true` 且省略游标重新订阅，清理已失效的时间线缓存和历史分页基线，应用新快照后按需向前分页。取消订阅仍用 `resident.unsubscribe`。

机器定义见 [WebSocket JSON Schema](project-task-websocket.schema.json) 的 `ResidentTimelineSnapshotEvent` 和 `ResidentTimelineUpdateEvent`。

## 连接与取消订阅

先从 `GET /api/agents/resident-conversation` 取得 ready 状态的 Agent/thread 绑定，再认证并等待共享 WebSocket 的 `ready`（`protocol_version:3`）。App 复用任务连接的鉴权、续期、心跳和接收循环。命令 `id` 为 8–128 字符，只接受字母、数字、下划线和连字符。Task 与 Resident 的订阅上限合计为 32 个。Resident 订阅计入连接活跃状态。

```json
{"type":"resident.unsubscribe","id":"resident-unwatch-0001","agent_id":"resident_agent_example","thread_id":"resident_thread_example"}
```

取消订阅返回 `result`，`data:null`。切页、取消订阅或断线后服务端仍继续执行。用户明确停止时调用 REST `POST .../cancel`。

## 重连、发送与错误

重连后重新认证，用已应用的游标续接原 scope。REST 负责发送、请求核对和停止。丢失发送回执时先查询原 `request_id`，确认未受理后才用同一 ID 补发。REST 历史按需分页，`GET .../conversation?limit=1` 可补读窗口之外的活动轮或复核原请求的 scope。详见 [Resident 接入说明](resident-agent-conversation.md)。

服务端每次订阅读取都会重验 owner、Agent/thread 绑定和可用状态；其他用户资源返回 404，管理员也不能越过私人会话隔离。

订阅失效只移除受影响的 scope，错误形状为：

```json
{
  "type": "error",
  "agent_id": "resident_agent_example",
  "thread_id": "resident_thread_example",
  "status": 404,
  "error": {"code":"NOT_FOUND","message":"资源不存在","details":{}}
}
```

命令错误带 `id`，通过 pending command 定位原 Resident scope；不要假设它一定另带 agent/thread。异步订阅错误使用 `agent_id + thread_id` 定位，不能因缺少 task_id 就关闭整个 socket、清掉其他 Task、审批观察或客户端工具。连接级认证失效继续按公共连接规则处理。
