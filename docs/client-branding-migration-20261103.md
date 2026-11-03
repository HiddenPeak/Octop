# APP 接线变更：Inhere-Box 命名迁移

更新：2026-11-03。适用于本仓库当前服务端和 Dashboard；APP 本轮只提供接线文档，没有修改 Swift/UI。部署是否包含本次改动须核对实际运行版本，不能只依据软件版本号判断。

## 客户端需要同步的名称

| 位置 | 原名称 | 当前名称 |
| --- | --- | --- |
| `TaskPlan.source` | `octop_planning` | `inhere_box_planning` |
| 原生执行引擎 `engine_id` | `octop` | `inhere-box` |
| 工具输出 JSON 的 UI 提示键 | `octop_ui` | `inhere_box_ui` |
| Skill frontmatter 的展示元数据命名空间 | `metadata.octop` | `metadata.inhere_box` |
| 备份 manifest / 恢复结果的软件版本字段 | `octop_version` | `inhere_box_version` |
| 宿主历史 MCP server 名 | `octop_task_history` | `inhere_box_task_history` |

`harness_todos`、`acp`、`renderer`、`version`、`data`、`data_ref`、`plan_id`、`step_id`、任务/项目 ID 及 REST/WS 路径保持当前契约。产品展示可使用 `Inhere-Box`，协议值按表格的精确大小写处理。

原生引擎选择示例：

```json
{"engine_id":"inhere-box","connection_id":null,"model_id":null}
```

计划来源示例（完整计划仍须带合法步骤及身份）：

```json
{"source":"inhere_box_planning","plan_id":"existing-plan","revision":1,"steps":[{"step_id":"existing-step","content":"执行任务","status":"pending"}]}
```

工具输出示例：

```json
{"inhere_box_ui":{"renderer":"demo_card","version":1},"data":{"value":42},"text":"已完成"}
```

大型 UI 输出继续支持 `data_ref="artifact"`：显式 `data` 优先，否则读取工具消息的 `artifact`。工具 ID/名称应作为不透明值处理，不根据旧品牌前缀决定是否显示或执行。

## HTTP、认证和 Bonjour（上一轮改动，客户端也须同步）

| 项目 | 当前值 |
| --- | --- |
| 滑动续期响应头 | `X-Inhere-Box-Access-Token` |
| 旧式专家选择请求头 | `X-Inhere-Box-Agent-Id` |
| 网页 Cookie 写操作 CSRF 请求头 | `X-Inhere-Box-CSRF` |
| WS 服务端诊断响应头 | `X-Inhere-Box-Connection-ID` |
| 网页管理员 Cookie | `inhere_box_session_<box-id-hash>` |
| SSO 状态 Cookie | `inhere_box_sso_state` |
| 原生设备证明 JWS `typ` | `inhere-box-proof+jwt` |
| 盒子访问令牌 JWT `aud` | `inhere-box-api` |
| Bonjour browse type / TXT product | `_inhere-box._tcp` / `inhere-box` |

HTTP 头名不区分大小写；Cookie、JWS `typ`、JWT `aud`、JSON 键和值使用精确字符串。设备仍原样签署服务端 challenge payload，不自行增删字段；challenge payload 没有要求客户端增加 `aud`。ES256 算法及 64 字节 `r||s` 签名格式保持原样。

原生 APP 使用 `Authorization: Bearer ...`；网页管理员使用 HttpOnly Cookie 和同源/CSRF。原来的 `X-Octop-*`、旧 Cookie 名及旧证明类型没有别名入口。认证模型版本仍为 2，盒子签名协议版本仍为 1；这些版本号不能代替本次名称迁移。

## 历史数据与客户端缓存

- 服务端读取历史 `octop_planning` 计划及 `engine_id=octop` 快照时，转换成当前模型并输出新名称；计划、步骤、客户端和业务记录的稳定 ID 保留。
- 旧插件的 `octop_ui` 输出仍可输入；运行时转换为 `inhere_box_ui`，新内置插件直接输出新键。Dashboard 也能读取历史旧信封；新键和旧键同时存在时，新键优先。
- 已落盘的原始事件、消息、检查点、用户文件和 APP 本地缓存不做整库字符串替换。客户端读取旧缓存或历史原始事件时，应兼容旧名称；提交新请求、保存新协议数据时使用新名称。不要替换用户正文、工具参数或任意 JSON 字符串中的品牌文字。
- 历史消息 `additional_kwargs` 中的 `octop_composer_context`、`octop_inbound_attachments`、`octop_stream_error`、`octop_task_image` 分别对应新的 `inhere_box_*` 键；工具 `artifact` 中的 `octop_task_file_changes` 对应 `inhere_box_task_file_changes`。若 APP 直接读取这些原始对象，也应双读、新写优先。常规 Task DTO 的 `file_changes`、正文块、附件字段未改名。
- 内部渠道绑定改为 `_inhere_box_task`，读取旧 `_octop_task` 配置仍可恢复项目和动作模式。APP 继续调用渠道的正式 DTO，不自行构造内部配置键。
- Dashboard 的 Cron 元数据辅助键也已改为 `inhere_box_*`，属于前端内部表示；正式 cron DTO 中的 `session_key`、`model`、`last_status` 等未添加品牌前缀。
- 旧备份 manifest 仍可恢复，新备份写 `inhere_box_version`。旧项目同步游标仍可校验，新游标用新签名域。
- 已登记的设备继续使用原私钥及 `client_id`，重新挑战换票即可，无需重新配对。网页需重新登录，跨升级未完成的 SSO 流程重新发起。

## 不改动的外部名称和存储定位符

外部依赖 `octop_harness`、`octop_gateway`、`octop_memory`、`octop_browser` 及其真实发布名称继续保留。Harness 的 `.octop/` 系统目录、已有主数据目录/数据库名的读取回退、专家记忆的 `octop_expert_<hash>` 存储命名空间及 Docker 的 `octop_sandbox` 默认定位符用于定位已有数据，不做目录或存储迁移；它们不是本轮新增客户端字段。腾讯新闻 CLI 的 `octop_tencent-news_0.1` 是对接方标识，保留真实值。

## 客户端核对顺序

1. 更新上述枚举、JSON 解码器、请求头、JWS 类型和 Bonjour 声明。
2. 对旧缓存/原始历史事件保留读取兼容，以新名称为优先；新写出只用新名称。
3. 用已有设备换票，核对 `box_id`；分别验证手动 IP 与 Bonjour 的 HTTP/WS 接入。
4. 验证新任务原生引擎、计划步骤、工具 UI 的普通输出和 `data_ref=artifact` 输出，以及历史计划/工具输出恢复。
5. 对照 [当前 WS Schema](project-task-websocket.schema.json)、[计划示例](examples/project-task-planning-turn.json) 和实际服务 `/api/openapi.json`。

网络地址、动态 IP 和断线恢复见 [APP 网络接入](app-network-connection-handoff.md)；认证完整 DTO 见 [盒子账户接线](box-local-account-client.md)。本次没有向远端部署、提交或推送代码。

## 桌面包旧版本标记：升级前迁移一次

独立脚本为 `scripts/migrate_portable_branding.py`。它将桌面运行包 `VERSION.txt` 中的 `octop_version=` 迁移为 `inhere_box_version=`，日常桌面版本解析不再读取旧标记。新打包产物已经写新标记；旧解压目录或待复用的旧 ZIP 升级前执行一次：

```sh
uv run python scripts/migrate_portable_branding.py "/path/to/portable"
uv run python scripts/migrate_portable_branding.py "/path/to/portable" --apply
```

第一条只预览；第二条创建带原文件哈希的旁置备份后替换。也可直接传 `VERSION.txt` 或 ZIP 文件路径。脚本重复运行不会再次改写已迁移文件，JSON 输出提供备份路径和原始 SHA-256。只处理版本标记，不对业务数据库、原始消息或检查点做替换；上文其他历史字段仍按各自读取兼容规则处理。完整操作说明见 [桌面说明](../desktop/README.md#one-time-version-marker-migration)。

## 本轮验证记录

`make all`：7525 passed、31 skipped；前端 TypeScript 与生产构建通过，相关前端测试 53 passed，接线集成测试 59 passed，最终迁移兼容测试 140 passed。独立版本标记迁移脚本 3 passed，并已演练 CLI 预览、执行和重复执行；桌面 Go 测试通过。本机没有旧桌面运行包需要执行实际迁移。当前业务数据库的 839 个轮次、115 个计划块已只读验证可恢复；其中 17 个旧引擎快照可转换为新名称。
