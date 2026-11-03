# Bonjour 局域网发现

APP 完整网络接入（局域网/外网地址、认证和WS）见[最新接线文档](app-network-connection-handoff.md)。本文描述服务发现细节。

`inhere-box run` 在主 API 监听成功后，自动通过 mDNS/DNS-SD 发布
`_inhere-box._tcp.local.`，进程正常停止时发送撤销记录并关闭广播套接字。
注册失败只记日志，不阻断 HTTP 服务。普通 HTTP、自备证书 HTTPS、TLS 双端口
都发布主 API；双端口的 HTTP 跳转/ACME 端口不单独发布。

## 启用与检查

`config.json` 的 `bonjour_enabled` 默认 `true`，环境变量
`INHERE_BOX_BONJOUR_ENABLED=false` 可关闭。需要重启服务生效。
监听地址仍由原有 `bind_host` / `INHERE_BOX_BIND_HOST` / `--host` 决定，
Bonjour 不会自动更改监听范围。

```bash
inhere-box run --host 0.0.0.0 --port 8088
```

仅监听 `127.0.0.1` / `::1` 时不广播。`0.0.0.0` 只发布本机 IPv4 网卡地址；
`::` 发布 IPv6 地址；显式绑定网卡 IP 时只发布匹配地址，并使用对应网卡广播。
端口从已监听的套接字读取，因此 `--port 0` 也会发布实际分配的端口。
地址在启动时采集，网卡或 IP 变化后需要重启服务重新发布。

在同一局域网的 Mac 上可以用系统工具检查：

```bash
dns-sd -B _inhere-box._tcp local.
dns-sd -L 'inhere-box <hostname> (8088)' _inhere-box._tcp local.
```

实例名取自实际浏览结果；重名时允许 Bonjour 自动改名。路由器需要允许
mDNS 组播（UDP 5353），服务端 TCP 监听端口也需要可达。容器发布的是容器
看到的地址和端口；桥接网络/端口映射不会自动转换为宿主机地址，局域网发现
应使用具备局域网组播与可达地址的网络配置。

## 客户端发现契约

| 字段 | 内容 |
|---|---|
| DNS-SD type | `_inhere-box._tcp` |
| Domain | `local.` |
| Instance name | `inhere-box <hostname> (<port>)`，重名可变，不作为稳定身份 |
| SRV target / port | 实际提供主 API 的 `.local.` 主机名和 TCP 端口 |
| A / AAAA | 与实际监听地址族匹配的本机网卡地址，不包含回环地址 |
| TXT `txtvers` | `1` |
| TXT `product` | `inhere-box` |
| TXT `version` | inhere-box 软件版本 |
| TXT `scheme` | `http` 或 `https`，与主 API 的实际 TLS 配置一致 |
| TXT `path` | `/`，表示基础 URL 的根路径；接口继续使用 `/api/...` |

广播不携带 token、账号、项目、文件路径或配对凭据。发现记录仅用于列出候选服务，
不代表身份可信或登录成功。连接后沿用现有登录、TLS 校验和能力协商；
不要用实例名或 TXT 版本替代服务端身份和能力接口。

服务端默认允许HTTP/WS，无需IP白名单或额外开关。IP变化不需要修改HTTP放行配置；
客户端按本次发现结果连接。网页登录同源校验使用本次实际访问地址，认证与CSRF继续生效。

## Mac / iOS APP 接线

APP 按下列契约实现或核对浏览与解析。本轮没有修改 Swift/UI；服务端广播验证
不替代 APP 的实际设备验收。

1. 使用 `NWBrowser` 的 Bonjour 浏览描述，type 为 `_inhere-box._tcp`，domain 为
   `local.`；需要 TXT 时使用包含 TXT 的浏览描述。
2. 处理新增、更新和移除结果，读取 TXT，解析 SRV 主机和端口，按 `scheme`、
   主机、端口及 `path` 构造基础 URL。不要猜固定 8088，也不要把 `0.0.0.0`
   当连接目标。IPv6 链路本地地址需要保留解析结果的接口作用域。
3. 用户选中服务后交给既有连接/登录流程；HTTPS 沿用证书信任策略，
   HTTP 沿用 APP 当前的局域网明文连接策略，不自动绕过证书校验或账号验证。
   现有证书若不覆盖发现的 `.local.` 主机名，需要使用与证书匹配的受信任地址
   或配置合适的证书；Bonjour 本身不解决 TLS 名称不匹配。
4. 按目标平台配置 `NSBonjourServices`（包含 `_inhere-box._tcp`）和
   `NSLocalNetworkUsageDescription`，沙盒 Mac APP 同时核对网络客户端权限，
   在真机验证局域网访问授权、发现、连接和服务退出后的列表移除。

Apple 接口与权限说明：
[NWBrowser](https://developer.apple.com/documentation/network/nwbrowser)、
[NSBonjourServices](https://developer.apple.com/documentation/bundleresources/information-property-list/nsbonjourservices)、
[NSLocalNetworkUsageDescription](https://developer.apple.com/documentation/bundleresources/information-property-list/nslocalnetworkusagedescription)、
[网络客户端权限](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.network.client)。
