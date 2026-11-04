# APP 网络接入：局域网、外网 IP 与 Bonjour

更新：2026-11-03。适用于当前 inhere-box 服务端，重点描述 `account_mode=box_local` 的原生 APP 接入。本文是网络接入入口；认证 DTO 见[盒子账户接线](box-local-account-client.md)，任务操作见[统一会话接线](agentcomputer-unified-conversations-handoff.md)。本轮不修改 APP Swift/UI。

客户端还须同步 [Inhere-Box 字段迁移清单](client-branding-migration-20261103.md)，包含计划来源、引擎枚举和工具 UI 信封。

## 1. 当前支持范围

| 连接方式 | APP 入口 | 服务端行为 |
| --- | --- | --- |
| 局域网自动发现 | 搜索局域网中的 inhere-box，选择后连接 | Bonjour 发布 `_inhere-box._tcp.local.`，提供协议、主机和实际端口 |
| 局域网 IP / 域名直连 | 手动输入地址和端口 | 使用同一套 REST / WS 接口，不需要 Bonjour 成功才能连接 |
| 外网 IP / 域名直连 | 手动输入外部地址和外部端口 | 协议支持直连；服务 TCP 端口须从外部可达，或由端口映射/反向代理提供 |
| HTTP / WS | 默认支持 | 无 IP、端口或主机名白名单要求，无额外 HTTP 开关 |
| HTTPS / WSS | 目标部署配置后可选 | 按实际 TLS 配置连接和验证证书，不由 APP 强制升级协议 |

局域网与外网使用相同的认证、REST 和 WS 协议，不是两套 API。外部 IP 直连同一实例也不是 Bridge，不需要建立远程专家或改变 Task 的主机归属。

`box_http_origins` 是旧配置兼容字段，不再控制 HTTP 放行。APP 不应要求用户填写该字段、固定 DHCP 地址、安装证书后才能使用 HTTP，或逐个授权新的 IP。

Bonjour 负责局域网服务发现，不提供公网搜索、NAT 穿透、端口映射或公网地址登记。局域网受 VLAN、访客网络或组播隔离影响时，保留手动地址入口。

## 2. 地址模型与 URL 规则

APP 至少提供两个入口：**搜索局域网**、**手动输入地址**。两者得到相同的连接对象，后续进入同一个探测、认证和业务流程。

下表仅为占位示例，不是需要写入配置的地址：

| 输入/解析结果 | 基础 URL | Task WS URL |
| --- | --- | --- |
| 局域网 IPv4 和端口 | `http://192.168.10.20:8088` | `ws://192.168.10.20:8088/api/project-tasks/ws` |
| 外网 IPv4 和映射端口 | `http://203.0.113.20:18088` | `ws://203.0.113.20:18088/api/project-tasks/ws` |
| Bonjour SRV 目标和端口 | `http://inhere-box-example.local.:8088` | `ws://inhere-box-example.local.:8088/api/project-tasks/ws` |
| 外部 HTTPS 部署 | `https://inhere-box.example.com:8443` | `wss://inhere-box.example.com:8443/api/project-tasks/ws` |
| IPv6 和端口 | `http://[2001:db8::20]:8088` | `ws://[2001:db8::20]:8088/api/project-tasks/ws` |

地址处理规则：

1. 保存协议、主机和端口，使用结构化 URL API 构造地址。IPv6 主机加方括号；链路本地 IPv6 保留解析得到的接口作用域。
2. 地址表单可预填 `http` 和 `8088`，但允许修改。用户输入完整 URL 时，省略端口遵循 URL 的 HTTP 80 / HTTPS 443 语义，不能悄悄改成 8088。
3. 基础 URL 是服务根地址，不含 `/api`；调用时追加准确路径，避免生成 `/api/api/...`。当前契约按根路径部署，任意反向代理子路径不在本轮已验证范围。
4. REST 和 WS 共用所选端点的主机及端口，只转换协议：`http → ws`、`https → wss`。不能将 WS 固定为 localhost、8088 或服务端内部地址。
5. 外网端口映射时使用客户端访问的外部端口，例如外部 18088 映射内部 8088，REST、WS、附件及产物都走外部 18088。
6. 不将 `0.0.0.0`、`::` 当成连接目标；它们是服务监听地址。`.local.` 地址仅作为局域网候选，不能当作外网地址。
7. 不在地址中放入管理密钥、分享码、访问令牌或用户密码。

一个盒子可以保存多个地址候选，例如 Bonjour 局域网端点和用户输入的外网端点。地址是连接位置，`box_id` 才用于关联安装身份；实例名、IP 和 Bonjour SRV 主机名都不是稳定账号标识。

## 3. Bonjour 搜索契约

### 3.1 搜索、解析与显示

| 项目 | 值/含义 |
| --- | --- |
| Browse type | `_inhere-box._tcp` |
| Domain | `local.` |
| 完整类型 | `_inhere-box._tcp.local.` |
| 实例显示名 | `inhere-box <hostname> (<port>)`；重名时可被自动调整 |
| SRV target / port | 主 API 的主机名和实际监听端口 |
| A / AAAA | 本机监听地址族对应的网卡地址，不含回环地址 |
| TXT `txtvers` | `1` |
| TXT `product` | `inhere-box` |
| TXT `version` | 服务端软件版本，仅用于展示，不代替能力查询 |
| TXT `scheme` | `http` 或 `https` |
| TXT `path` | 当前为 `/` |

Mac/iOS 可使用 `NWBrowser` 的 Bonjour 浏览描述；需要读取 TXT 时使用带 TXT 的描述。浏览结果本身不等于完整 HTTP URL，还需要解析 SRV 目标、端口和地址。解析规则见 [Bonjour 详细说明](bonjour-discovery.md)。

APP 应持续处理新增、更新和移除事件，校验 `product` / `txtvers` / `scheme`，以实例名显示候选服务。用户选择后，根据本次解析结果构造基础 URL，进入第 4 节探测流程。

同名服务可能来自不同设备；同一设备也可能有多个接口地址。不能仅按显示名合并盒子，不能把某个网卡 IP 永久写入客户端代码。解析得到多个地址时，允许尝试其他可达候选，避免首个地址不可达就认定整个服务离线。

服务关闭后的移除事件只影响候选在线状态，不删除已保存的盒子、设备私钥或业务缓存。拒绝局域网权限、没有搜索结果和连接超时都不代表设备凭据失效。

### 3.2 动态地址与当前限制

- APP 重新进入搜索、网络变化或重连时重新浏览/解析，不永久缓存解析出来的 IP。
- 已登记盒子的Bonjour SRV主机名由持久`box_id`派生，服务重启后保持稳定；首次设置前使用临时主机名，实例名可能因重名调整。
- 服务端每5秒检查网卡及地址变化；局域网切换、DHCP换地址后自动重建Bonjour广播和组播套接字，断网或发布失败后持续重试，保留主机名和端口。APP需重新解析并重连，实际恢复时间还受广播和缓存影响。此能力要求HTTP监听通配地址；显式绑定旧IP的HTTP服务不会自动迁移。HTTP本身没有IP白名单，无需修改放行配置。
- 不把 Bonjour 可发现写成一定可连接，也不承诺跨路由器、跨 VLAN 或公网搜索。

### 3.3 APP 权限与 HTTP 支持

Mac/iOS APP 在 `Info.plist` 中声明 `NSBonjourServices` 包含 `_inhere-box._tcp`，并配置 `NSLocalNetworkUsageDescription`。沙盒 Mac APP 同时核对网络客户端权限。首次搜索应在可展示系统权限提示的用户操作中触发，区分“权限被拒绝”和“未找到服务”。平台要求见 [Apple 本地网络隐私说明](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)。

APP 的网络策略必须同时支持局域网 IP、`.local` 和外网 IP 的 HTTP/WS；不能只放行 localhost。若使用 Apple ATS，需要核对目标系统与现有 `NSAppTransportSecurity` 配置，不能把 `NSAllowsLocalNetworking` 当成所有外部 HTTP 请求都可用的保证。Apple 说明了 `NSAllowsArbitraryLoads` 的全局 HTTP 行为，以及部分细粒度键存在时对它的覆盖关系；按最终 `Info.plist` 实测这些入口，避免组合配置使 HTTP 仍被系统拒绝。参见 [ATS 全局例外](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowsarbitraryloads)。选用 HTTPS 时仍保留正常证书验证。

## 4. 所有入口共用的连接流程

```text
Bonjour 选择 / 手动输入地址
  → GET /api/health
  → GET /api/box/status
  → 校验盒子身份和认证模型
  → 新设备登记，或已有设备签名换票
  → GET /api/box/account
  → 能力查询和列表加载
  → 建立唯一 Task WS，等待 ready
  → 提交消息、订阅正文和客户端工具通知
```

1. `GET /api/health` 只证明实例有响应，不能视为登录成功。当前盒子模式响应可以只有 `{"ok":true}`，不能要求一定包含旧的 `db/users_loaded` 字段。
2. `GET /api/box/status` 无需令牌。当前检查 `account_mode=box_local`、`protocol_version=1`、`auth_model_version=2`，并读取 `box_id`、`state`、`multiple_owner_devices`。
3. `state=claimed` 表示管理密钥已配置，不表示本客户端已登记；`unclaimed` 与 `recovery_required` 按[账户说明](box-local-account-client.md)处理，不自动绕过登记或重置身份。
4. 已保存盒子换地址后，先核对返回的 `box_id`。身份不一致时作为另一安装处理，不发送原盒子的凭据或沿用原业务缓存；Bonjour TXT 不提供认证证明。
5. 只有明确的模式响应才能选择认证分支。当前 `server` 模式的盒子状态接口返回 `404` 且 `error.details.reason=box_mode_required`；普通代理404或HTML错误页不能当作模式判断。超时、连接失败、401、403、服务异常都不能触发“降级为旧账号登录”或自动删除设备密钥。`server` 模式继续使用原服务端账号协议，不套用盒子管理密钥协议。

切换局域网和外网地址不必重新登记同一台设备。使用原盒子的设备私钥与 `client_id` 换票，再读取账号上下文；不能因 IP 改变再次创建自有设备。

## 5. 原生 APP 认证接线

### 5.1 首次登记

| 使用场景 | 流程 |
| --- | --- |
| 自有设备 | 本机生成 P-256 密钥 → `POST /api/box/owner-claims` 提交管理密钥及候选公钥 → 签署返回 challenge → `POST /api/box/pairings/{pairing_id}/proof` → 取得 `client_id` |
| 受邀设备 | 本机生成独立密钥 → `POST /api/box/pairings` 提交一次性分享码及候选公钥 → challenge 持钥证明 → 取得独立 guest 的 `client_id` |

自有设备支持多台，协议角色仍为 `primary`；不得根据已存在 `primary_client_id` 禁止添加其他自有设备。受邀端不需要分享者二次审批。保留本次登记的 `request_id` 和原请求内容用于重传，响应丢失按账户文档的 result 挑战流程恢复，不重复登记。

### 5.2 日常换票

```http
POST /api/auth/challenges
Content-Type: application/json
```

```json
{"client_id":"已登记的客户端ID","action":"session","params":{}}
```

使用设备私钥原样签署返回的 payload，向 `POST /api/auth/sessions` 发送：

```json
{"proof":"compact ES256 JWS"}
```

签名头为 `alg=ES256`、`typ=inhere-box-proof+jwt`，访问令牌的 `aud=inhere-box-api`；签名使用 64 字节 `r||s`，不使用 DER，不自行增加或修改 challenge 声明。成功取得 `access_token`、`paired_client_id`、`session_kind=client` 等字段，当前 `expires_in=600`。挑战当前有效期 60 秒且单次消费；失效后重新获取，不重放旧 proof。精确 DTO 与其他登记步骤见[账户接线](box-local-account-client.md)。

本次协议名统一为 `Inhere-Box`：滑动续期响应头使用 `X-Inhere-Box-Access-Token`，按需选专家的旧式接口使用 `X-Inhere-Box-Agent-Id`，WS 服务端诊断响应头使用 `X-Inhere-Box-Connection-ID`。网页写操作使用 `X-Inhere-Box-CSRF`，Cookie 为 `inhere_box_session_<box-id-hash>`，SSO 状态 Cookie 为 `inhere_box_sso_state`。原生 APP 仍使用下方 Bearer 协议。旧版 APP 必须同步更新这些协议名称；保留设备私钥和 `client_id` 后重新换票即可，无需重新配对。完整映射见[命名迁移](inhere-box-renaming.md#httpcookie-与签名协议)。

REST 与原生 WS 握手均使用：

```http
Authorization: Bearer <access_token>
```

原生 APP 保留设备私钥、盒子身份和 `client_id`，不把管理密钥或已消费分享码用作日常 token。没有长期 refresh token；过期前重新挑战换票。服务冷启动会使旧会话失效，使用原登记密钥重新换票，不重新认领盒子。

原生 APP 不使用网页管理员 Cookie 协议。网页管理仍是管理密钥换取 HttpOnly Cookie，并执行同源及 CSRF 校验。

## 6. 缓存、REST 与 WebSocket

认证后调用 `GET /api/box/account`，按服务端返回的 `(box_id, instance_id, account_id)` 隔离业务缓存；角色变化后重新换票、重读账号并切换缓存，不能仅按 IP 或 URL 归属项目和任务。

APP 可维护“安装身份 + 地址候选 + 设备凭据引用 + 当前连接代次”。这些是客户端概念，不是新增的服务端请求 DTO。切换地址时终止旧 receiver 和在途观察任务，确保旧连接的异步结果不写入新连接代次；保留未确认业务请求的原 request_id 和请求体，不因切网自动创建重复任务或重试工具副作用。

业务接口继续使用统一协议：

| 用途 | 接口 |
| --- | --- |
| 当前账号归属与角色 | `GET /api/box/account` |
| Task 协议能力 | `GET /api/project-tasks/capabilities` |
| 模型、专家等资源 | `GET /api/project-tasks/resources` |
| 项目 | `/api/projects` |
| 全部任务第一页 | `GET /api/project-tasks?scope=all` |
| 多轮任务和操作 | `/api/project-tasks/...` |
| Task 与 Resident 正文主连接 | `/api/project-tasks/ws` |
| 显示时区 | `GET /api/settings/timezone` |

普通会话仍为同一个 Task 的多轮记录，项目和专家可选，不恢复 `/api/agentcomputer/v1`。能力结果核对 `contract=unified-task-conversations`、`version=1`、`protocol_version=3`；具体功能还需读取资源和 Task 自身能力，不能只比较软件版本。

盒子模式下，WS 握手带 Bearer，成功后服务端直接发送：

```json
{"type":"ready","protocol_version":3}
```

等待 ready 后再发送命令或恢复订阅。不要发送旧 authenticate 首帧，不将 token 放入 URL。保持一个 receiver 分发回执、正文和通知；业务 request_id、WS 命令 id、正文 cursor 分别保存。断线后使用有效会话或新票据重建 WS，并按游标恢复观察，不自动重新执行已发送意图。完整报文见[任务 WS 指南](project-task-websocket.md)和 [JSON Schema](project-task-websocket.schema.json)。

## 7. 外网地址与部署边界

APP 手动录入公网 IP / 域名和外部端口，直接访问同一服务，无需为外网另加服务端 HTTP 白名单。公网访问仍取决于实际网络可达性：路由器映射、运营商网络、防火墙和反向代理由部署环境提供；服务绑定 `0.0.0.0` 不等于已建立公网入口。

有反向代理时，保留原生认证使用的 Authorization 请求头，除 REST 外也必须转发 WebSocket Upgrade 和长连接，支持附件/产物及所需请求大小。若由代理提供 HTTPS，APP 使用代理入口的 HTTPS/WSS 地址，不使用后端 HTTP 内部地址。

Bonjour 不发布或推断公网映射地址，局域网解析结果不能自动当成外网候选。只有用户已经配置并确认属于同一盒子的端点，才能作为切网后的候选地址。

## 8. 错误处理与验收

| 现象 | APP 行为 |
| --- | --- |
| 没有 Bonjour 结果 | 区分权限、网络隔离和空结果；允许手动输入，不删除已有连接 |
| IP 超时/拒绝连接 | 显示连接不可达，尝试重新解析或已保存的同盒子候选；不当作凭据失效 |
| HTTP 被 APP 系统网络策略阻止 | 检查 APP ATS/网络权限，不要求用户给服务端固定 IP 或开启 HTTPS |
| 401 / WS 鉴权失败 | 使用原设备私钥重新挑战换票；根据实际错误区分过期、撤销、身份变化，不循环重试 |
| 403 | 检查角色、同源、CSRF 和具体 reason；当前版本不会因普通 HTTP 返回 `box_tls_required` |
| `box_id` 与保存值不同 | 停止复用凭据和缓存，按另一安装确认处理 |
| 服务重启 / Bonjour 目标变化 | 重新发现、核对身份、换票和重建 WS；不重新登记自有设备 |

APP 验收至少覆盖：

1. 同一局域网两台真实设备完成 Bonjour 搜索、解析、选择、认证和 Task WS ready。
2. 用户拒绝局域网权限后仍能进入手动地址入口，并能在授权恢复后重新搜索。
3. 手动局域网 IPv4、可用时的 IPv6、非 8088 端口均能连接，不依赖硬编码 IP。
4. 服务端换 IP 并重启重新发布后，APP 解析新地址，复用原盒子设备登记与账号缓存归属。
5. 同一盒子的公网映射 IP/域名和外部端口完成 REST、WS、附件/产物访问；在实际外网完成验收，不能只测同路由器的回流地址。
6. HTTP/WS 默认可用；HTTPS/WSS 部署按实际证书连接；两种入口均保留账号与权限检查。
7. 多自有设备、guest、票据到期、服务重启换票，以及局域网/外网地址切换不造成重复登记或重复业务执行。
8. 服务退出后搜索列表移除在线候选，保存的盒子和设备密钥保留。

## 9. 当前验证记录

2026-11-03，本机 8088 已从 `release/1.0.0@b2e94ae5` 加本轮未提交的 Bonjour 和默认 HTTP/WS 改动启动，监听 `0.0.0.0:8088`。实际网卡 IP 与 Bonjour 目标上的 HTTP 健康检查均通过，未登录业务接口继续返回 401，原盒子身份及业务记录数量保持不变。原生 `dns-sd` 的发现、解析、停止撤销已验证；最终 `make all` 为 **7510 passed / 31 skipped**。

当前运行记录为 `.runtime/8088/http-default-restart-latest.json`，它记录的是当次运行，不是固定 APP 地址配置；不要把其中的 IP、PID 或随机 Bonjour 目标写入 APP。服务端其他实例需部署相应代码后才能具备相同行为。

本轮没有提供或验证公网映射地址，也没有修改 APP Swift/UI，因此不把公网真机接入或 APP 端到端发现列为已完成验收。本次文档整理只核对契约和链接，不重复执行上一轮完整代码回归。
