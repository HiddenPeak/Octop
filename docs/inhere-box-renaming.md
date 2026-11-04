# inhere-box 命名迁移

更新：2026-11-04。

本地产品及内部入口统一使用以下名称：

| 入口 | 新名称 |
| --- | --- |
| 产品展示名 | `Inhere-Box`（允许使用小写 `inhere-box`） |
| Web/PWA、桌面壳、安装包标识 | `inhere-box` |
| Python distribution / CLI | `inhere-box` |
| Python import / `python -m` | `inhere_box` |
| 环境变量前缀 | `INHERE_BOX_` |
| 新安装默认数据目录 | `~/.inhere-box` |
| 新数据库、日志、向导口令文件 | `inhere-box.db`、`inhere-box.log`、`inhere-box-login.txt` |
| systemd / launchd 服务名 | `inhere-box` |
| 桌面 bundle ID | `com.inhere.box` |
| Bonjour type / TXT product | `_inhere-box._tcp` / `inhere-box` |
| Bonjour 主机名 | `inhere-box-<hostname>-<随机后缀>.local.` |

当前源码安装：在仓库运行 `uv sync`，然后使用 `uv run inhere-box run`。也可以从本地构建的 wheel 安装。改名本身不代表已经向 PyPI、GitHub Releases、容器仓库或安装脚本下载站发布新产物；上游下载地址和原项目链接保持真实地址。

## 已有安装

`INHERE_BOX_*` 优先，未设置时接受对应的 `OCTOP_*`。`INHERE_BOX_HOME` 或旧的 `OCTOP_HOME` 可以显式选择数据目录。

已有安装采用一次性迁移：停服后将安装根目录 `.octop` 迁到同级 `.inhere-box`，将 `octop.db` 迁为 `inhere-box.db`，同步更新配置与数据库中的本机安装路径。正常运行只使用新目录和新库名；发现旧根目录、旧库或未完成的迁移时，会要求先完成迁移，不再回退打开旧数据位置。

先停止该实例的服务、驻留 worker 和其他写入进程，在当前源码或已安装本版本的 Python 环境中执行：

```sh
uv run python scripts/migrate_installation_branding.py --root /path/to/.octop --destination /path/to/.inhere-box
uv run python scripts/migrate_installation_branding.py --root /path/to/.octop --destination /path/to/.inhere-box --apply
```

第一条只做只读预检，第二条取得离线锁后复制到同级临时目录，在副本中迁移并校验 SQLite，再启用新目录。原目录归档为同级 `.octop.before-inhere-box-<id>`，完整保留原数据库、附件和身份；新目录中另有旧库文件的迁移备份。应预留至少一份完整数据目录的可用空间。迁移进度保存在父目录 `.inhere-box-directory-migration.json`，中断后重跑相同命令恢复。新旧目录或新旧库独立并存时拒绝覆盖，不自动合并。迁移完成后重复执行不会重建数据库。

路径更新只作用于指向旧安装根目录的明确字段：服务端项目目录、专家工作目录和本地存储、轮次配置中的服务端目录、SQLite 记忆路径、技能路径、ACP 与本地 MCP 启动配置、历史归档的控制库身份，以及 `config.json` 和 `env`/`.env` 中已知的本机路径项。客户端目录、远端存储路径、业务正文、原始事件、检查点内容和 SDK 内部 `.octop` 子目录不做品牌字符串替换。盒子身份、账户、密钥和任务 ID 保持原值。

这套目录迁移处理根目录内的 SQLite 控制库。PostgreSQL 的路径记录位于外部数据库，当前脚本会拒绝该目录迁移，需要单独安排外部数据库中的路径更新；不会连接或改写外部数据库。

迁移完成后，将外部部署配置中的 `INHERE_BOX_HOME` 改为新目录。根目录内的 POSIX venv 生成启动脚本会更新路径；目录外的 shell wrapper、systemd unit、launchd plist 和容器挂载需要在部署时更新或重装。Windows venv 启动器若嵌入旧路径，迁移会报错，须先移走旧 venv 并在新目录重建。进程环境中显式指定旧 `DATABASE_SQLITE_PATH` 时也须先更新；数据根目录中的对应 env 文件由目录迁移器更新。不要在迁移未完成时启动服务或手工改写临时副本。

自定义数据根目录不需要改名时，可不带 `--destination`，仅迁移该目录中的旧数据库文件。`migrate_portable_branding.py` 仍只负责桌面包 `VERSION.txt`，不负责安装数据目录迁移。FnOS 与 Docker 启动入口会在初始化或启动服务前调用安装迁移模块，失败时中止启动；原来只挂载 `.octop` 子目录的容器须先改为父级挂载或在宿主机离线迁移，参见 [FnOS 升级说明](../fnos/README.md)。

Python 的公开导入入口是 `inhere_box`，CLI 主命令是 `inhere-box`；自行编写的扩展需要同步入口。浏览器本地偏好在同一站点首次加载新版本时复制到新前缀，保留原值。

## HTTP、Cookie 与签名协议

按本次要求，客户端与服务端统一切换以下名称，不再发送或接受旧请求头、旧 Cookie 或旧设备证明类型：

| 原名称 | 当前名称 |
| --- | --- |
| `X-Octop-Access-Token` | `X-Inhere-Box-Access-Token` |
| `X-Octop-Agent-Id` | `X-Inhere-Box-Agent-Id` |
| `X-Octop-Box-CSRF` | `X-Inhere-Box-CSRF` |
| `X-Octop-Connection-ID` | `X-Inhere-Box-Connection-ID` |
| `inhere_box_box_session_<box-id-hash>` | `inhere_box_session_<box-id-hash>` |
| `inhere_box_sso_state` | `inhere_box_sso_state` |
| JWS `typ=octop-box-proof+jwt` | `typ=inhere-box-proof+jwt` |
| 访问令牌 JWT `aud=octop-box-api` | `aud=inhere-box-api` |

HTTP 头名称不区分大小写；JWS 类型、audience 与 Cookie 名按这里的精确值处理。设备证明的 `alg=ES256`、签名算法和接口路径不变。APP 更新后沿用已登记的设备私钥和 `client_id`，重新请求挑战并换票，无需重新配对。旧版 APP 必须更新协议名称。网页需重新登录，升级前未完成的 SSO 流程需重新开始。

浏览器 CSRF 的内部签名域改为 `inhere-box-browser-csrf-v1`。项目同步游标新签名域改为 `inhere-box-list-sync-v2`；只在读取已有游标时兼容旧签名域，不影响实例、账号、用途或签名校验。品牌更改不重置已配对设备、账户、任务或密钥。

## 自有字段的后续改名

`TaskPlan.source`、原生 `engine_id`、工具 UI 信封、Skill 展示元数据和备份版本字段也已切换。客户端字段映射、历史缓存读取要求及无需修改的 DTO 见 [客户端迁移清单](client-branding-migration-20261103.md)。

## 保留的兼容标识

以下名称属于既有数据或外部协议，继续保留读取/交互能力：

- 外部依赖 `octop-harness`、`octop-gateway`、`octop-memory`、`octop-browser` 及其 Python 模块、SDK 类名和环境约定。
- Harness 工作区的 `.octop/`、旧 `.octop-auth`、专家记忆存储命名空间及 Docker 默认存储定位符；它们定位现有数据，不进行物理搬迁。
- 历史计划、旧引擎快照、旧插件 `octop_ui`、旧消息元数据及旧备份 manifest 仅保留读取兼容；新写出使用当前名称。旧 React/JSX 全局入口继续服务已安装插件。
- 上游仓库、版权归属、实际历史 Git 分支/提交和第三方服务约定。

Bonjour 客户端需要把浏览类型及 `NSBonjourServices` 更新为 `_inhere-box._tcp`，并接受 `product=inhere-box`。端口、接口、HTTP/WS 支持和认证流程保持当前行为；参见 [APP 网络接入文档](app-network-connection-handoff.md)。新名称在服务重新启动后发布，主机名继续动态生成，不包含固定局域网 IP。

容器升级时核对宿主机数据目录与挂载关系：需要迁移时，让迁移器同时看到同级的新旧目录；不要将旧安装目录独立挂载为不可重命名的挂载点。安装包和 App ID 改名本身不代表宿主机数据已经完成迁移。


## 2026-11-04 安装迁移验证

`make all` 通过：7749 passed、31 skipped；Ruff 和全量严格 mypy 通过。新增验证覆盖完整根目录备份、SQLite WAL、路径字段与历史身份、加密 MCP、venv、迁移中断恢复、目标丢失拒绝启动，以及 FnOS/Docker/安装器入口和自有仓库镜像接线。开发验证使用临时实例；随后按用户要求完成本机安装目录迁移和服务重启，业务记录与盒子身份核对一致，原目录完整归档；未部署远端。

## 2026-11-03 改名轮次验证

- `make all`（含自有标识改名）：7525 passed，31 skipped；Python 严格类型检查通过。
- 补充的备份、游标与静态资源验证：114 passed；旧目录/变量、旧备份名称和旧游标签名的迁移验证：7 passed。
- 协议字段相关前端验证：31 passed；计划/工具 UI/引擎相关前端验证：53 passed。迁移补充验证：140 passed，接线集成验证：59 passed。
- 桌面版本标记的一次性迁移脚本：3 passed（普通文件、压缩 ZIP、备份、幂等、字段优先级）；桌面 Go 测试通过，日常版本解析不再读取旧标记。
- 当前数据库只读演练：839 个轮次、115 个计划块均能读取，17 个旧引擎快照转换为新标识。
- 前端 TypeScript 与生产构建、桌面 Go 测试通过。wheel 的模块为 `inhere_box`，CLI 入口为 `inhere-box`，内含新品牌前端。
- 前端全量测试在当前 Node 26 上需要关闭实验 WebStorage。相同设置下，改名前对照为 19 个失败；改名后的首轮为 21 个失败。新增的目录显示测试预期已修正，AuthGuard 单独复验通过，相关两文件合计 26 passed。原有 mock/DTO、语音和超时问题仍可在改名前源码复现，本次没有顺带修改这些功能。
- 本机 8088 已使用新入口重启；Bonjour 发布 `_inhere-box._tcp` / `product=inhere-box`。LAN 与 `.local` HTTP 健康检查通过，未认证业务请求仍返回 401；重启前后盒子身份、数据库版本与业务记录数量一致。

2026-11-03 验证时未提交、未推送，也未部署到远端；当时本地安装包位于 `dist/inhere-box-20261103/`。
