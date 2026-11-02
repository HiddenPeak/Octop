# 盒子账户：管理密钥、自有设备和分享码接线

更新：2026-11-02。适用于显式启用 `account_mode=box_local` 的实例。默认 `server` 模式保留原账户协议。服务端与Dashboard已改造；App本轮仅更新此文档，不修改Swift/UI。架构、数据模型与运维规则见[技术设计与实施说明](box-local-account-pairing-plan.md)，精确DTO以部署的 `/api/openapi.json`、`/api/docs` 为准。

协议兼容：认证模型版本仍为 2，签名协议仍为 1；`GET /api/box/status` 新增 `multiple_owner_devices=true`。客户端不得再根据 `primary_client_id` 是否非空禁止添加自有设备。`primary_client_id` 已弃用，仅保留一个代表设备 ID，不用于授权或工具路由；实际权限取决于已认证客户端的角色。`GET /api/box/account` 的 `owner_client_ids` 返回活动自有设备列表（受邀端返回空列表）。

新客户端使用 `/api/box/owner-claims` 登记自有设备、`/api/box/clients/{id}/owner` 授予自有设备权限。旧 `/primary-claims` 保留为弃用的登记兼容入口。旧 `/clients/{id}/primary` 转移接口经管理员认证后返回 `410 box_primary_transfer_retired`，不改变任何设备权限；客户端必须显式使用新 `/owner` 接口授予共享访问，不能自动重放旧转移请求。界面将“主设备”改为“自有设备”，说明“共享我的项目和会话”；“受邀设备”继续说明独立空间。旧服务未返回多设备能力时，不承诺支持多台自有设备。

## 1. 三种身份

- `admin`：管理密钥登录网页换取独立Cookie，不登记浏览器为客户端。
- `primary`：兼容保留的协议角色名，产品显示为“自有设备”。允许多台，均访问同一个主人账户；管理密钥登记不消耗密钥。
- `guest`：使用一次性分享码加入的独立客户端，数据归自己的内部用户身份，无网页管理权限。

一个客户端对应一个密钥对和 `client_id`。原生客户端不保存管理密钥或已消费的分享码，只保存自己的私钥、盒子身份及client_id。管理密钥不要分享给其他人。

`GET /api/box/status` 无需会话。`state=unclaimed` 表示尚未配置管理密钥；`claimed` 表示已配置，不代表已有自有设备；`recovery_required` 表示信任不可用。认证模型版本为2；签名协议仍为1。旧客户端不能仅依据 `claimed` 判断自己已经取得主人权限。

旧模型/信任不一致的实例可能拒绝启动，因此不能依赖一定能访问status来修复。不要将连接失败自动当成server模式；只有明确404可按旧部署方式处理。

## 2. 首次管理密钥与本机维护

维护命令与服务的运行环境必须指向同一个 `OCTOP_HOME`。首次启动盒子服务建立控制数据库、稳定身份和库外信任记录，然后停止这个实例，在可信本机终端执行：

```sh
octop box key init
```

输出一次长期管理密钥；请安全保存，之后再启动服务。数据库只存摘要。命令必须取得与服务端相同的daemon锁，运行中的服务会导致命令拒绝，不能绕过锁写库。这里的“本机”指部署Octop的主机，不是任意客户端电脑。

已使用旧061配对模型的实例，备份并停服后明确执行：

```sh
octop box key init --upgrade-legacy
```

此操作先校验原信任，保留主人与业务数据，撤销旧客户端/会话/分享材料。旧浏览器不转为自有设备。原 `octop box pair` 入口退役。

丢失管理密钥时，在可信本机停服执行 `octop box key rotate`；如需同时撤销客户端，加 `--revoke-clients`。运行中的网页管理员也有轮换入口。普通服务器多用户实例不能直接套用此升级命令。

## 3. 传输与持钥证明

生产环境使用HTTPS/WSS并正常验证盒子服务身份。只有实际连接来自回环地址才允许HTTP/WS供本机开发；不能通过Host或转发头伪造例外。盒子URL、box_id用于定位和标识，不单独证明TLS服务身份；原生端如采用证书固定，必须通过可信渠道取得对应材料，不能全局跳过验证。

每个原生客户端安装生成独立P-256密钥；公钥JWK仅提交 `kty="EC"`、`crv="P-256"`、`x`、`y`。坐标为32字节无填充base64url。私钥保存在系统安全存储中，不上传、不跨客户端复用。

挑战包含 `challenge_id`、完整 `payload` 和 epoch秒 `expires_at`。payload绑定 `box_id`、challenge、nonce、action、params_hash、subject、auth_revision、boot_id、exp。签署前核对盒子、主体、动作和参数符合当前意图；不能修改payload或加claims。

签名为compact JWS，受保护头只能是 `{"alg":"ES256","typ":"octop-box-proof+jwt"}`。ES256 签名值采用 64 字节的 `r||s`，不能直接使用 DER 编码签名。`params_hash` 为参数对象规范JSON（排序键、无额外空白、保留Unicode）的UTF-8 SHA-256十六进制摘要。挑战有效期60秒、只消费一次；响应丢失时申请新挑战，不重放旧proof。

## 4. HTTP接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/box/status` | 读取初始化与认证模型状态。 |
| `POST /api/auth/browser/sessions` | 同源网页提交管理密钥，签发管理员Cookie。 |
| `GET /api/auth/browser/session` | 恢复Cookie会话元数据和CSRF token。 |
| `POST /api/auth/browser/refresh` | 管理Cookie+CSRF续建会话，撤销旧会话。 |
| `DELETE /api/auth/browser/session` | 撤销当前管理员会话并清除Cookie。 |
| `POST /api/box/owner-claims` | 管理密钥授权新的自有设备候选。 |
| `POST /api/box/pairing-grants` | 网页管理员或自有设备创建一次性受邀分享码。 |
| `POST /api/box/pairings` | 分享码绑定受邀候选公钥。 |
| `POST /api/box/pairings/{id}/proof` | 候选持钥证明；成功直接登记，无审批。 |
| `POST /api/box/pairings/{id}/result` | 候选签名查询结果，处理成功响应丢失。 |
| `POST /api/auth/challenges` | 申请session、enroll或result动作挑战。 |
| `POST /api/auth/sessions` | 已登记客户端签名换取10分钟Bearer。 |
| `GET /api/box/account` | 当前访问身份、角色、稳定缓存标识。 |
| `GET /api/box/clients` | 有管理权限的身份读取客户端及角色状态。 |
| `POST /api/box/clients/{id}/revoke` | 网页管理员撤销客户端，包括最后自有设备。 |
| `POST /api/box/clients/{id}/owner` | 网页管理员将活动客户端指定为自有设备。 |
| `POST /api/box/management-key/rotate` | 网页管理员轮换管理密钥。 |
| `POST /api/auth/logout` | 撤销当前会话。 |

审批队列及旧approve/cancel接口通过必要认证后返回410；未认证或无权限的请求仍可能先返回401/403。不要继续轮询等待审批。管理密钥、分享码与签名证明放 JSON 请求体，访问令牌放 Authorization 头或由浏览器携带 Cookie；都不放 URL 或日志。请求拒绝未知字段，响应客户端应兼容新增字段。设备撤销、指定自有设备和网页退出成功返回 `204` 空响应，客户端不要继续解析 JSON。

## 5. 原生客户端流程

### 5.1 明确认领自有设备

已经确认盒子地址后，原生端生成密钥并提交：

```json
{
  "management_key": "主人输入的长期管理密钥",
  "request_id": "这次认领稳定的随机标识",
  "public_key_jwk": {"kty": "EC", "crv": "P-256", "x": "...", "y": "..."},
  "name": "我的电脑",
  "platform": "macos"
}
```

发送到 `POST /api/box/owner-claims`。返回pairing_id及候选挑战，用本机私钥签署，再请求 `/api/box/pairings/{pairing_id}/proof`。只有响应 `state=consumed` 且有client_id才表示登记成功；此后另行执行session挑战换票。清除输入的管理密钥，不把它用作日常API token。

成功持钥证明时登记自有设备，已有自有设备不影响继续登记，也不会被降级或断开。多个候选独立完成，每个公钥只能登记一次。自有设备候选窗口为300秒，配对响应没有顶层expires_at；其中challenge.expires_at只表示这次60秒签名挑战的有效期。

### 5.2 分享给其他客户端

网页管理员以Cookie+CSRF提交 `{"request_id":"..."}` 到 `/api/box/pairing-grants`。自有设备提交同一request_id和新鲜 `enroll` proof；proof的params为 `{"request_id":"..."}`。新生成的secret自生成起有效2天（48小时，172800秒），单次使用。客户端以返回的expires_at作为实际到期时间；已签发记录保留原期限，不自动延长。重复签发request_id不会再次返回原secret，响应丢失时创建新request_id。

新客户端生成独立密钥，将下列内容发送到 `/api/box/pairings`：

```json
{
  "secret": "收到的一次性分享码",
  "request_id": "本次候选稳定的随机标识",
  "public_key_jwk": {"kty": "EC", "crv": "P-256", "x": "...", "y": "..."},
  "name": "受邀电脑",
  "platform": "macos"
}
```

不需要复制包含grant_id、时间等元数据的JSON。协议仍兼容可选grant_id；显式给错ID时不回退查找。候选挑战证明后立即成为guest，不需要分享者再次批准。

同次请求重试保留request_id、公钥、name和platform。已绑定分享码不能换公钥；消费后不能登记第二个端；过期、撤销和已使用公钥都不能绕过检查。

### 5.3 结果恢复与日常会话

若proof响应丢失，申请 `{"pairing_id":"...","action":"result","params":{}}` 挑战并签署，通过 `/result` 读取结果。未完成窗口过期返回expired；已完成窗口仍可返回consumed及原client_id，不创建第二个客户端。consumed只证明曾登记成功，不保证该客户端当前尚未被撤销；仍需后续会话换票。

已有client_id请求 `{"client_id":"...","action":"session","params":{}}` 到 `/api/auth/challenges`。签署后向 `/api/auth/sessions` 发送 `{"proof":"..."}`，得到access_token、session_id、paired_client_id、role、user_id、session_kind=client、expires_in=600。

原生HTTP和WS握手使用 `Authorization: Bearer …`。Task WS鉴权成功先返回ready，不需要再发送登录首帧。令牌到期前重新签名换票，再按订阅游标重连；没有长期refresh token，不接受URL token或WS旧authenticate首帧。普通断线不代表设备被撤销。

## 6. 网页登录

`POST /api/auth/browser/sessions` 请求只有 `{"management_key":"..."}`。要求同源Origin；若携带仍有效的管理员Cookie，还需要对应CSRF。失效或过期Cookie不阻止重新输入管理密钥登录。原生proof和分享码不能替代management_key。

Cookie为HttpOnly、SameSite=Strict，HTTPS下Secure，Path=/api；服务端校验独立管理员会话，paired_client_id为null。响应只包含会话元数据与CSRF token，不返回可由JS持久保存的管理令牌。后续写操作发送 `X-Octop-Box-CSRF`。

默认网页会话24小时，页面重开可以恢复未失效Cookie。当前Dashboard不自动调用refresh接口，也不保存管理密钥自动重新登录。退出、清除浏览器存储、到期或服务重启后重新输入密钥；这不会影响管理密钥长期有效，也不会新增浏览器设备。

轮换在单独页面进行，新密钥仅保存在该页面内存一次展示。轮换成功后旧Cookie失效，可以保存新密钥再登录。管理密钥不经过router state、URL或Web持久存储。

## 7. 权限、自有设备授权与缓存

guest拥有独立内部用户身份，只能操作自己的项目、任务和附件。允许使用任务独立空间内的文件工具，以及绑定本client_id的客户端工具；不开放盒子管理、任意服务器目录、远程工作台、主机shell、ACP、专家、插件、MCP、子代理或远程fetch。资源目录只提供可用模型和受限能力。

`GET /api/box/account` 提供当前user、role、session_kind及 `(box_id, instance_id, account_id)`。缓存必须以此组合作为归属范围，不依赖URL或固定用户名。role改变后先重新换票并读取account，再切换缓存；不能继续展示前一个身份的历史。

将受邀设备设为自有设备只允许网页管理员。仅目标端旧会话及未使用分享材料失效，client_id与私钥保留；重新换票后映射主人。其他自有设备的会话、身份和分享码不变。目标端原受邀数据保留，不合并进主人历史。重复授权不撤销会话。

撤销客户端不删除项目或任务；恢复使用须新密钥重新登记，不能复用已撤销公钥。通过分享码重新登记会创建新的受邀身份，不会自动接回原受邀身份的数据；主人用管理密钥登记新的自有设备后，仍访问原有主人数据。当前没有“换机恢复原受邀身份”的接线协议。

轮换请求 `{"revoke_clients":false}` 默认保留客户端及其原生会话，撤销所有Web会话和全部未完成授权，包括分享材料和待完成的自有设备认领。true同时撤销全部客户端，需要重新认领/分享。轮换或移除最后自有设备后，当前有效管理密钥仍可登录网页。

## 8. 长连接、执行身份与恢复

服务端持续重验WS和长响应，失效连接关闭；Task受理和客户端工具在领域入口再次校验。client.attach与本次会话的paired_client_id一致，不允许客户端冒用另一电脑ID领取工具任务。撤销不代表已派发动作未执行，也不能自动将未知结果转交另一设备重试。

服务冷启动使旧会话失效；原生客户端保留登记密钥，重新换票即可。网页重新使用管理密钥登录。库外信任版本防止恢复旧数据库后静默恢复已撤销权限；信任损坏或不一致必须通过可信本机维护处理，不能删除trust文件后自动开放认领。

通用备份恢复入口会拒绝已有盒子状态或包含盒子状态的快照，返回 `409 box_restore_requires_recovery`。本轮没有实现协调数据库与库外信任的恢复工具；`key rotate` 和 `--upgrade-legacy` 同样拒绝损坏的信任，不能作为绕过方式。

本轮不实现旧多用户服务器自动迁移、跨客户端同步同一个受邀人的数据、物理按键找回、转让盒子或恢复出厂。需要这些能力时单独确定数据与授权语义。

## 9. 错误处理约定

错误使用统一 envelope，结合 HTTP 状态、`error.code` 与 `error.details.reason` 判断；不要依赖本地化 `message` 文本。下表为主要账户错误，精确响应结构见实例 OpenAPI 与错误处理约定。

| HTTP 状态 | reason 示例 | 客户端处理 |
| --- | --- | --- |
| 400 / 422 | `public_key_invalid`、`pairing_parameters_invalid` 或 DTO 校验错误 | 修正字段或公钥编码，不自动生成新授权绕过错误。 |
| 401 | `session_invalid`、`proof_invalid`、`management_key_invalid`、`pairing_invalid` | 区分失败步骤：会话到期申请新挑战，证明不可重放，错误密钥要求重新输入，失效分享材料要求重新邀请。重新换票仍失败时停止自动重试。 |
| 403 | `management_required`、`box_guest_forbidden`、`box_origin_required`、`box_csrf_invalid`、`box_tls_required` | 核对身份、同源、CSRF 和传输条件；重复登录不能扩大权限。 |
| 404 | `client_not_found` 或业务资源不存在 | 更新本地列表；不能以错误详情推断其他人的业务数据。 |
| 409 | `request_conflict`、`public_key_already_registered`、`grant_request_already_completed` | 重试保持原候选请求参数；已登记或撤销公钥不可重复注册。分享签发响应丢失时用新 request_id 重新签发。 |
| 410 | `box_approval_retired`、`box_primary_transfer_retired` | 移除旧审批与独占主设备转移；由用户明确选择新的自有设备授权流程。 |
| 429 | `rate_limited`、`pairing_capacity`、`challenge_capacity` | 延迟重试，避免不断生成候选或挑战。 |
| 503 | `recovery_required`、`trust_unavailable` | 停止自动认领或重新注册，转交盒子本机维护。 |

`proof_invalid` 也可能由挑战过期、已消费、服务重启或认证版本变化造成。不能仅凭此错误删除本地私钥或认定设备已被撤销。部分通用认证或 DTO 校验失败不包含 `details.reason`，应保留对 HTTP 状态和 `error.code` 的处理。
