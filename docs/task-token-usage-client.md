# App 任务 Token、盒子／三方来源与参考费用

接口更新日期：2026-10-30；参考单价快照仍为 2026-10-23。此文描述新增的任务来源汇总与每日趋势接口；原 `/api/usage/summary`、调用明细和 Excel 导出保留各自口径。

## 接口与权限

使用 inhere-box 登录令牌 `Authorization: Bearer <access_token>`，不是模型服务商的 API Key。

| GET 接口 | 范围 |
| --- | --- |
| `/api/usage/task-summary` | 当前登录用户自己的数据，不接受切换他人身份。 |
| `/api/admin/usage/task-summary` | 仅管理员，默认全部用户；可加 `user_id`。 |

参数：

| 参数 | 说明 |
| --- | --- |
| `window` | 默认 `last_30d`；支持 `today/yesterday/last_7d/last_30d/all`、`day:YYYY-MM-DD`、`month:YYYY-MM`、`range:YYYY-MM-DD:YYYY-MM-DD`。日期范围包含结束日期。 |
| `task_id` | 精确筛选一个任务。 |
| `project_id` | 精确筛选项目；空字符串表示未关联项目。 |
| `offset` | 从 0 开始，默认 0。 |
| `limit` | 1–100，默认 100。 |

```http
GET /api/usage/task-summary?window=range:2026-10-19:2026-10-25&limit=50
Authorization: Bearer <access_token>
```

“本周”由客户端按服务端 `timezone` 算出日历日期，再传 `range`。`last_7d` 是滚动七天，不能代替本周。

响应 `range_start/range_end/generated_at/items[].last_usage_at` 均为 **Unix 毫秒**；查询区间为 `[range_start, range_end)`。这与原 usage summary 的秒时间戳不同。

## 每个任务的字段

| 字段 | 用途 |
| --- | --- |
| `task_id/title/project_id/project_name` | 稳定身份与当前可用名称；资源已删除时流水仍保留 ID，名称可能为空。 |
| `agent_id/agent_name/agent_attribution` | 关联常驻智能体及归属依据，见下文。 |
| `savings` | 本地／盒子模型节省的参考 API 费用估算；复用参考费用金额与状态结构。 |
| `call_usage` | 本任务新采集的逻辑模型调用合计。 |
| `sources.box` | 新流水中盒子调用的已知用量、缺失情况与参考费用。 |
| `sources.third_party` | 新流水中三方调用的同类数据。 |
| `retained_task_usage` | 历史轮次、标题与审核中保留的用量，独立于调用流水。 |

`box/third_party` 是产品接入来源，**不是本地／云端部署位置**。App 对应列应使用“盒子”“三方”。

### 新调用流水

`call_usage`、`sources.box`、`sources.third_party` 使用同一结构：

| 字段 | 语义 |
| --- | --- |
| `calls` | 已记录的逻辑模型调用数，SDK 内部 HTTP 重试不拆分。 |
| `statuses` | 各终态或运行状态的调用数。取消、失败不自动删除已报告用量。 |
| `tokens` | 输入、输出、总量、未缓存输入、缓存读／写、推理七个字段的已知合计。 |
| `missing_calls` | 每个 Token 字段分别有多少次调用缺少已知值。 |
| `derived_calls` | 每个字段中有多少次是可推导值，而非适配器明确报告。 |
| `coverage` | 每个字段的 `recorded/partial/not_collected/no_data`。仅描述已保留记录，不保证历史全量。 |
| `attribution_counts` | `direct` 直接任务归属、`submission` 通过已受理请求关联、`unassigned` 未归属。 |
| `reference_cost` | GPT-6 Astra 参考费用，见下文。 |

显示规则：

- `tokens.field=null` 为未知；`calls=0` 且 `coverage=no_data` 为无记录。
- 已知数值可以是明确的 `0`；不得把 null 转为 0。
- `coverage=partial` 时数值只是已知部分，显示“已知 N，另有 M 次未知”。
- 服务商在取消前没有返回 usage 时，调用仍保留，但 Token 和费用不能伪造。取消后迟到的可信 usage 可以补到同一条调用。
- 缓存是输入细分，推理通常是输出细分，不再重复加进 `total_tokens`。

主回答、判难、标题、进度说明、审核等实际模型消耗均可出现。Task 创建前的判难没有 `task_id`，通过同用户的唯一已受理 `submission_id/request_id` 关联回任务；没有受理结果或归属冲突时保持未归属，不伪造任务。

### 历史任务优化

`retained_task_usage` 返回 `input_tokens/output_tokens/total_tokens/turns/usage_records`，以及：

| 字段 | 语义 |
| --- | --- |
| `source_attribution` | `inferred/partial/unknown/no_data`。 |
| `sources.box/third_party` | 按每条旧轮次、标题或审核保存的模型引用推断来源后的用量。 |
| `unattributed_usage` | 无法推断来源的已知 Token，继续保留，不能强行分给三方。 |
| `reference_cost` | 历史业务记录口径下的独立参考估算。 |
| `cannot_add_to_call_usage=true` | 历史记录和新流水可能重叠，禁止相加。 |

每条历史记录的模型引用必须唯一匹配保留的 Provider 配置，且配置创建时间不晚于该记录。`kind=box` 推断为盒子，其余已知类型推断为三方；缺失、重建或歧义时保持未知。当前 Provider 类型可能后来被修改，所以这始终是**历史配置推断**，不会改写旧账或冒充原始调用回执。

旧任务没有混合路由，也可能多轮切换模型，因此逐条归属，不能只用任务当前模型给整个任务贴标签。

App 可采用以下展示方式：

1. 有新流水时，来源列显示 `sources`，标“新调用流水”。
2. 没有新流水时，显示 `retained_task_usage.sources`，标“历史配置推断”；未归属量另行提示。
3. 始终保留 `retained_task_usage.total_tokens` 供核对。跨采集起点延续的任务可能同时有两套不同范围，不能认为任一套都保证完整，也不能两套相加。

## 统一参考费用

用户指定所有来源统一对标 **GPT-6 Astra Standard**。不是 Box、GLM 或其他服务商的实际账单，也不代表硬件、电费或订阅摊销。

参考价快照：`openai-gpt-6-astra-standard-2026-10-23-v1`，USD／百万 Token。

| 输入长度 | 普通输入 | 缓存读取 | 默认 30 分钟写入 | 12 小时写入参考价 | 输出 |
| --- | ---: | ---: | ---: | ---: | ---: |
| ≤272,000 | 10 | 1 | 12.50 | 25 | 50 |
| >272,000 | 20 | 2 | 25 | 50 | 75 |

按每次调用的输入量选择整次调用档位。输入计价分项互斥；推理已含在输出中，不再次收费。官方依据：[模型价格与长上下文规则](https://developers.openai.com/api/docs/models/gpt-6-astra)、[API 价格](https://developers.openai.com/api/docs/pricing)、[缓存规则](https://developers.openai.com/api/docs/guides/prompt-caching)、[推理用量](https://developers.openai.com/api/docs/guides/reasoning)。

明确假设与限制：

- 新流水按已知输入、输出和缓存读取计算。未报告缓存写入时不模拟 OpenAI 特有的写入；已报告写入但没有 TTL 时，使用默认 30 分钟，并在 `assumptions` 标注。
- 新流水缺少必要计价字段时，返回部分小计或未知，不把缺失 usage 填成 0。
- 历史业务记录没有缓存细分，参考估算不计缓存优惠、不模拟写入，并明确标注假设。历史记录是多调用累计；输入合计不超过 272,000 时，可以确认所含调用均未超过长上下文阈值。超过时无法判断各调用档位，以全部短上下文价／全部长上下文价计算参考费用上下界，返回 `estimated_range`，原因 `per_call_context`；不把累计量当成一次长调用。
- 历史数据按本次价格快照重新估算，不声称使用调用当时的价格。接口不在每次请求时联网抓价。
- 不包含工具、存储、税费、地区附加费、Fast／Flex／Batch 等其他服务档位。

`reference_cost` 金额均为十进制字符串：

| 字段 | 客户端处理 |
| --- | --- |
| `amount_usd` | 仅全部记录都能估算时非空。 |
| `known_amount_usd` | 可估算部分的小计。 |
| `status=estimated` | 显示“参考费用 $N”。 |
| `status=estimated_range` | 显示 `lower_bound_usd` 至 `upper_bound_usd` 的参考费用区间，单点金额仍为 null。 |
| `status=partial` | 显示“已知参考费用 $N，另有未知部分”，完整金额仍为 null。 |
| `status=unknown/no_data` | 显示未知／无记录，不能显示 `$0`。 |
| `missing_fields/assumptions` | 缺项及估算假设，可供详情说明。 |

新流水计数为 `estimated_calls/estimated_range_calls/partial_calls/unknown_calls`；旧业务记录计数为 `estimated_records/estimated_range_records/partial_records/unknown_records`，不把旧轮次数量冒充模型调用次数。有未知消耗时上界可为 null，不把已知下界冒充完整金额。

响应 `pricing` 返回参考模型、快照版本、核对日期、短／长上下文单价和官方 URL。`capabilities.reference_cost=true`、`capabilities.savings=true`；`actual_cost/execution_location/budget=false`。

## 智能体与本地模型节省（2026-10-29）

App 的“智能体”列以 `items[].agent_id` 对接实际身份。若与已加载的默认常驻智能体绑定相同，复用 App 现有 Hippogenius 品牌身份展示；其他身份显示 `agent_name`，空名称时回退 `agent_id`，两者皆空才显示未提供。不要读取 `selected_expert_id` 代替常驻智能体，也不要因为没有选专家而显示未提供。服务端的默认内部名称可能仍为“生活工作管家”，不要为品牌展示而改写历史身份或把所有智能体硬编码为同一个名称。

- `agent_attribution=delegation`：读取任务最早委派记录中的来源智能体，优先于默认智能体。
- `agent_attribution=owner_primary`：没有委派记录时，显示所属用户的默认常驻智能体（例如 Hippogenius）。这表示产品展示归属，不证明它执行了所有模型调用。
- `agent_attribution=unavailable`：没有可用归属；接口不会创建智能体或修改历史记录。名称来自当前可用身份，不能冒充调用时名称快照。不同用户的默认身份分别解析。

用户确认的节省口径为 **本地／盒子用量 × 现有参考单价**，三方调用不计入。接口以 `box` 来源作为该口径下的本地／盒子范围，仍不声称能够探测实际物理部署位置。

- 每个任务读取 `items[].savings`；全范围合计读取 `totals.savings`，不能只加当前页。
- 一个任务在所选日期有调用流水时，只用该任务流水估算节省；没有流水才用历史留存记录。总计逐任务遵守此选择，禁止将两套记录相加。
- 仅有三方调用时节省为明确的 `0`，即使三方未报告用量也不影响其不计入节省。没有记录时仍为 `no_data`。
- 本地／盒子缺少用量时保持 `partial/unknown`，不能把已知小计当完整节省。旧记录无法判定来源时节省保持未知；超过上下文阈值的旧累计量继续使用参考区间。
- `savings` 使用 `amount_usd/known_amount_usd/lower_bound_usd/upper_bound_usd/status/missing_fields/assumptions`，显示规则与 `reference_cost` 一致。节省列建议标注“本地模型节省估算（USD）”，不再固定显示“未支持”。
- 这是省去的参考 API 费用估算，不是实际账单差额；不扣除硬件、电费、订阅成本，沿用 `pricing` 的价格快照。

本次服务端与 Web 已接线；App Swift/UI 需要消费上述新增字段，此文是其接线约定。只读核对现有 App 后，具体接线点为：

1. `Services/Box/InhereBoxTaskUsageModels.swift`：`InhereBoxTaskUsageItem` 增加智能体字段和 `savings`；`InhereBoxTaskUsageTotals` 增加 `savings`。新增字段按可选值解码，保持旧服务端兼容。
2. `Features/Tokens/TokenTaskUsageFormatter.swift`：`presentation` 当前把智能体固定为 `Strings.Tokens.notProvided`，需替换为上述身份解析；节省当前只有 `unsupported/notCollected` 占位，应依据 `capabilities.savings` 与实际 `savings.status` 显示估算、区间、部分已知、未知或无数据。
3. 默认智能体品牌可复用 `Core/Strings.swift` 的 `Strings.Workspace.agentName` 及现有身份展示逻辑（当前为 Hippogenius · Lite），不要把服务端内部名称当作产品品牌。
4. 页面节省合计使用 `totals.savings`，不复用仍采用旧口径的 `/usage/summary` 的 `capabilities.savings=false`。

## 每日 Tokens 与累计节省图接口（2026-10-30）

原 UI 定义为：左侧按日堆叠本地／云端 Tokens，右侧显示所选期间累计节省曲线和总额。原型中的数组、金额与云端柱高倍率不属于统计规则。现在使用以下接口获取真实留存用量，替换两张图的“未支持”占位：

```http
GET /api/usage/task-trend?window=month:2026-10
GET /api/admin/usage/task-trend?window=month:2026-10&user_id=123
```

普通接口强制仅本人；管理员接口可省略 `user_id` 统计全部用户。两者支持 `task_id`、`project_id`，不分页。`window` 默认 `last_30d`，支持 `today/yesterday/last_7d/last_30d/day:YYYY-MM-DD/month:YYYY-MM/range:YYYY-MM-DD:YYYY-MM-DD`。`range` 包含起止自然日；最多覆盖 366 个自然日，不接受 `all`，无效或超限返回 400。滚动 30 天可能跨越 31 个自然日；需要自然月图时传 `month`。

| 字段 | 图表接线 |
| --- | --- |
| `days[].date` | 服务端时区的 `YYYY-MM-DD`，升序、补齐所有日期。不要再按手机时区换日。 |
| `days[].sources.box.tokens.total_tokens` | 当日盒子／本地柱高，使用真实 Token 比例。 |
| `days[].sources.third_party.tokens.total_tokens` | 当日三方柱高，与盒子使用同一纵轴，不乘放大倍率。 |
| `days[].sources.unattributed` | 不能推断来源的历史用量，单列提示，不归入盒子或三方。 |
| `days[].savings` | 当日盒子省去的参考 API 费用估算。 |
| `days[].cumulative_savings` | 从本次查询起点到当日的累计节省，用于右图折线／面积；不是终身累计。 |
| `totals.sources` | 整个查询范围选中记录的来源总计。 |
| `totals.savings` | 右上角总额，与最后一天 `cumulative_savings` 相同。 |
| `pricing` | 与任务表一致的参考单价快照、单位 USD 和假设。 |
| `timezone/range_start/range_end` | 服务端时区和 `[start,end)` 毫秒区间；首尾日可能只覆盖部分时间。 |
| `excluded_unassigned_calls` | 未能关联任务而排除的流水条数；这些用量不属于图表或任务表总计。 |

每个来源对象返回 `tokens`、逐字段 `coverage`、`missing_records`、`receipt_calls` 和 `historical_records`。`tokens` 包含输入、输出、总量、缓存与推理字段；历史记录只提供输入／输出／总量，其余字段保持未采集，不能补造。`receipt_calls` 是模型调用条数，`historical_records` 是历史业务记录条数，两者不能都叫调用次数。历史来源始终是配置推断。

**去重在整个请求区间逐任务执行，再按日期分桶**（`selection_basis=per_task_receipts_precede_journals`）：某任务在整个区间有流水，则该任务整个区间只用流水，不在其他日期补入它的历史记录。无流水的任务才使用历史记录。不同任务可以分别采用两种来源，因此无需两套图表，也不能再把原始 `retained_task_usage` 加进图表。缩小日期区间可能改变任务的数据选择；客户端应请求完整所选区间，不逐日请求后拼接累计。

在相同筛选条件和记录状态下，`totals.savings` 与 `/usage/task-summary` 的 `totals.savings` 一致。两个请求间若产生新记录则可能暂时不同；`generated_at` 为生成毫秒时间，不是冻结账本。`recording_scope=retained_records_only`、`history_complete=false`，不保证采集启用之前的完整历史。

空日期仍返回一条数据，Token 为 null、coverage 为 `no_data`、当日节省为 `no_data`；累计值延续之前日期，无历史累计时同为 `no_data`。确实报告的零用量才是数值 0；只有三方调用时节省明确为 0，即使用量缺失也是如此。本地缺项会让金额保持 `partial/unknown`，历史长上下文不确定时保持 `estimated_range`，后续日期的累计也保留这些状态。图表可将 `no_data` 画为空柱，但提示必须是“无记录”；`partial` 只画已知量并提示缺失，`unknown` 不画成完整的零消耗。区间估算用范围带或区间提示，不把下界当成精确折线。

App 后续接线点（本次只新增服务端接口，不修改 Swift/UI）：

1. 在 `Services/Box` 增加趋势响应 DTO 和请求方法，按上述契约读取 `days/totals/pricing`。
2. `Features/Tokens/TokensViewModel.swift` 在所选日期变化时请求一次 `task-trend`，与任务表共用日期／项目／任务范围。
3. `Features/Tokens/TokensView.swift` 的 `dailyChart` 使用两个来源的 Token 堆叠；`savingsChart` 使用累计金额和范围总额，按状态展示缺失／区间。
4. 这两张图依据新接口的 `capabilities.source_breakdown/savings=true`；不要继续用旧 `/usage/summary` 的 `execution_location` 或 `savings=false` 控制图表。`execution_location=false` 仍正确，盒子／三方是产品来源，不是物理部署探测。

## 总计、分页与边界

- `totals` 在分页前计算，覆盖全部匹配任务；不能使用当前页相加替代。
- `total_tasks` 为匹配任务数，`next_offset` 非空时继续请求；更换筛选清空 offset。
- `unassigned_call_usage/unassigned_sources` 单列无法归属任务的调用，未计入任务 totals。传任务／项目筛选时不会把未归属调用硬归到该资源。
- 按最近用量时间、任务 ID 倒序；状态可能更新，分页不是冻结账本。
- 调用流水按模型开始时间归属；历史业务记录沿用原创建时间口径。跨日任务的两套日期归属可能不同。
- 仅有保留记录的任务参与统计。删除任务可能减少旧业务记录统计，独立流水可继续保留；没有源用量的导入历史无法补造。
- 原 summary 与 Excel 尚未切换到此口径。不要把旧 summary、旧业务记录和新流水三者相加。

运行时字段与完整响应类型以 `/api/docs` 和 `/api/openapi.json` 为准。接口变更不修改 Token 配额、金额预算或模型实际计费。
