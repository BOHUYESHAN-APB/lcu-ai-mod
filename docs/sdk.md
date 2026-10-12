# LCU Companion Integration SDK

LCU SDK 让外部系统接入一个正在运行的 Minecraft AI 同伴，而不是绕过人格和记忆创建另一套机器人。

接口分为三组（`backend/api/http_surface.py:54-60` 的 `SDK_INTERFACES`；`GET /api/sdk/info` 会在运行时拿它与真实路由表求交集，所以这三组里列出的端点都是真的挂着的）：

- `Gateway`：`POST /api/sdk/chat`、`GET /api/sdk/context`、`POST /api/sdk/context`
- `Observer`：`GET /api/status`、`GET /api/session`、`GET /api/memory`、`GET /api/config`
- `Actuator`：`POST /api/sdk/command`

SDK API 版本可通过 `GET /api/sdk/info` 查询，当前为 `1`（`http_surface.py:37`）。

> [!IMPORTANT]
> **26.3 的服务端就是这么多。** 本文件上一版列出的 V2 面（Skill Registry、外部控制租约、持久化 Task Run、事件游标、日程、记忆管理 REST）在本仓库**没有服务端实现**：`http_surface.py` 只挂了 **5 条**路由 —— `GET /api/sdk/info`、`POST /api/sdk/chat`、`POST /api/sdk/command`、`GET /api/v2/control`、`GET /api/v2/audit`。
> 结论是「**有 SDK 的客户端代码，没有 SDK 的 V2 服务**」：`backend/sdk/python_client.py` 与 `browser_client.js` 仍按旧的 master 面调用整套 `SDK_ENDPOINTS` 表，其中大部分会 **404**。用 `GET /api/sdk/info` 拿 `implemented` / `missing` 两个列表，不要凭客户端方法名猜服务端有什么。

## 扩展与适配器（设计约定，仓库内暂无实现）

SDK 核心保持传输和实现中立。第三方能力应当通过版本化 adapter manifest 接入，
而不是把特定 STT、TTS、语音客户端、墓碑、饰品或权限模组写死在核心 API。
manifest 至少声明：

- adapter ID、语义版本和兼容的 LCU API 范围；
- 可发现 capability，例如 `voice.input.stt`、`voice.output.tts`、`inventory.curios`；
- 输入/输出 schema、权限域、数据流向、失败码和取消语义；
- 是否需要客户端、服务端或外部进程；
- 项目许可证、源码地址、署名和分发义务。

上游必须先进行能力协商，缺少 adapter 时收到明确的 `unsupported`，不能假设
所有环境都具备同一能力。LCU 不承诺无条件兼容任意实现；目标是让遵循 manifest、
安全和版本契约的实现可以独立集成。集成者必须遵守对应开源许可证，尤其不能通过
动态加载、网络边界或重新打包规避 GPL/AGPL 的适用义务。

语音 adapter 当前仅预留接口，不进入主线实现（仓库里没有 manifest 加载器或语音代码）。

## 安全模型

后端默认监听 `127.0.0.1:8080`。如果通过 `WEB_HOST=0.0.0.0` 或其他非回环地址开放，
**必须**设置 `SDK_API_TOKEN`，否则启动直接抛错（`backend/main.py:29-39`）。浏览器跨域调用必须把完整 origin 配置到 `SDK_ALLOWED_ORIGINS`，多个 origin 使用逗号分隔。

建议使用 URL-safe token，例如：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。

REST 客户端使用：

```http
Authorization: Bearer <SDK_API_TOKEN>
```

WebSocket 的非浏览器客户端使用 `Authorization` 请求头；浏览器控制台通过 `lcu-token.<SDK_API_TOKEN>` 子协议认证。

需要从浏览器打开受保护的内置控制台时，使用 URL fragment：`http://127.0.0.1:8080/#token=<SDK_API_TOKEN>`。Fragment 不会发送到 HTTP 服务器或写入访问日志。

> **注意**：`GET /api/sdk/info` 自报 `auth_required = false`（`http_surface.py:189-191`），因为 26.3 **没有 SDK token 中间件**（master 有）。上面这套 `SDK_API_TOKEN` 目前只作用于"是否允许监听非回环地址"这一层，不构成对已开放端口上 REST 调用的鉴权。

### Java wire 的 token（现状）

Java 模组的 wire server 默认只监听 `127.0.0.1`（`ModConfig.wireBindAddress` 默认 `"127.0.0.1"`）。
`wireToken` 配置键（`ModConfig.java:24-27`，默认空 = 不鉴权）**在启动时真的武装**：
`LCUMod.commonSetup` 在 `WIRE.start()` **之前**调用 `WireServer.setRequiredToken(配置值)`，并打日志
（武装了报位数、空值打 **WARN** 明说端口不鉴权）。`WireServer` 里有完整闸门
（`WireServer.java:236,278-291`：首帧必须是匹配的 `hello`，未鉴权连接有 5 秒读超时 `:218`）。

也就是说：**空值 ⇒ 老行为（不鉴权，回环足够安全）；非空 ⇒ 后端首帧必须带同一个 token，否则连接被关。**

后端那一半：`backend/protocol/wire_client.py:293-306` 会从环境变量 **`LCU_WIRE_TOKEN`**、否则从文件
`.local/wire_token` 解析可选 token，并把它作为首帧 `{"type":"hello","token":...}` 发出（`:353`）。
（没有 `MOD_WIRE_TOKEN` 这个变量。）**两边要用同一个值**；不一致表现是连不上/反复重连。
回归在 `verifyWalk` 的 `wireTokenArmedChecks`。

## REST API

以下端点**都在 `backend/server.py` 或 `backend/api/http_surface.py` 里真实注册**。

### SDK 面（`backend/api/http_surface.py`）

- `GET /api/sdk/info` — SDK 版本、三组接口的可用性、`implemented` / `missing` 端点清单
- `POST /api/sdk/chat` — 通过人格、记忆与 Planner 发送消息
- `POST /api/sdk/command` — 直接向已连接的客户端身体发送动作（受保留命令表限制）
- `GET /api/v2/control` — 只读：当前控制模式与活动租约
- `GET /api/v2/audit` — 只读：审计事件（26.3 目前只有 `security` 一类）

其中：

- `POST /api/sdk/command` 拒绝保留命令 `control_external` / `control_builtin` / `toggle_ai` / `shutdown`（`http_surface.py:42`），也拒绝 `__lcu_` 前缀的参数（`:47`）。
- `GET /api/v2/control` 恒返回 `mode = "builtin"`、`coordinator = null`：26.3 没有"外部接管"模式，控制权只认左脑五个身份（`backend/agent/control_lease.py:55-60`）。**不存在** `POST /api/v2/control/leases`（申请）/ `…/heartbeat`（续租）/ `…/release`（释放），也不存在 `POST /api/v2/player-pairings`。

### 核心后端面（`backend/server.py`）

- `GET /api/status` — 读取后端状态
- `GET /api/llm/providers` — 返回服务商预设
- `GET /api/llm/roles` — 返回 agent 角色列表
- `GET /api/llm/config` / `POST /api/llm/config` — 读取 / 保存某个 agent 的 LLM 配置
- `GET /api/llm/models` / `POST /api/llm/models` — 按当前配置远程拉取模型列表
- `GET /api/persona` / `POST /api/persona` — 读取 / 更新默认人设
- `GET /api/sdk/context` / `POST /api/sdk/context` — 读取 / 写入外部注入上下文
- `GET /api/status`、`GET /api/session`
- `GET /api/memory` — 读取当前记忆摘要、玩家关系、最近任务结果（**只读**，见下）
- `GET /api/config` / `POST /api/config` — 读取 / 更新运行配置
- `GET /api/permissions` / `POST /api/permissions` — 分层策略（master / friend / guest + ReplyGate）
- `GET /api/tasks`、`POST /api/tasks/activate`、`POST /api/tasks/cancel` — 任务列表与状态机
- `GET /api/commands` / `POST /api/commands` — 指令白名单与闸门配置
- `GET /api/tools`、`GET /api/composites`、`GET /api/governance`、`POST /api/jobs/cancel`
- `GET /api/tokens`、`GET /api/database`、`GET /api/database/messages`、`GET /api/database/search`
- `GET /api/contacts`、`GET /api/contacts/{uuid}/messages`、`POST /api/contacts/{uuid}/send`、`POST /api/contacts/{uuid}/read`
- `WS /ws` — 状态/聊天推送（唯一的 websocket）

### 明确**不存在**的端点（会 404）

`GET /api/v2/info`、`GET|POST /api/sdk/identity`、`GET /api/v2/body-requests/{request_id}`、
`GET /api/v2/skills`、`GET /api/v2/skills/{id}`、`POST /api/v2/skills/{id}/runs`、
`GET /api/v2/task-presets`、`GET /api/v2/task-presets/{id}`、`POST /api/v2/task-presets/{id}/runs`、
`POST /api/v2/control/leases*`、`POST /api/v2/player-pairings`、
`GET /api/v2/runs`、`GET /api/v2/runs/{id}`、`POST /api/v2/runs/{id}/cancel`、`POST /api/v2/runs/{id}/resume`、
`GET /api/v2/events`、`GET|POST /api/v2/schedules`、`PATCH|DELETE /api/v2/schedules/{id}`、
以及全部 `/api/v2/memory*`。

`backend/tests/test_http_surface.py:122-129` 把这些断言在 `missing` 集合里，
`http_surface.py:185-187` 也自注了这一批"在 26.3 没有真实数据源"。

**记忆**：唯一 HTTP 面是只读 `GET /api/memory`（`server.py:531`），读的是活路径的 `agent/memory.py`。
`agent/memory_management.py` / `memory_catalog.py` 存在但**不在活路径**（没有任何模块 import 它们），
所以没有记忆的浏览/分类/导出/摘要/归档/软删除 REST。

**身体诊断**：`POST /api/sdk/command` 返回的 `request_id` 是进程内调试记录，无法通过任何 HTTP 端点回查
（`/api/v2/body-requests/{request_id}` 未注册）。

## PowerShell Headed Test

仓库内 `scripts/headed-test.ps1` 有 5 个模式（`-Mode`：`Status` / `Discover` / `Raw` / `Skill` / `Preset`，默认 `Status`）。

**只有 `Status`（只读）和 `Raw`（走 `/api/sdk/command`）现在能跑通**：

```powershell
pwsh -File scripts/headed-test.ps1 -Mode Status
pwsh -File scripts/headed-test.ps1 -Mode Raw -Command get_state
```

`Discover`、`Skill`、`Preset` 会打 `/api/v2/skills`、`/api/v2/task-presets`、`/api/v2/control/leases`、
`/api/v2/skills/{id}/runs`、`/api/v2/task-presets/{id}/runs`、`/api/v2/runs/{id}`、`/api/v2/body-requests/{id}` —— 这些端点**都不存在**，
必然 404（脚本本身没坏，是服务端这一批没搬）。

脚本的 `-BaseUrl` 默认 `http://127.0.0.1:8080`；远程使用必须 HTTPS。

## Python

```python
from sdk import LCUClient

with LCUClient("http://127.0.0.1:8080", api_token="optional-local-token") as client:
    print(client.get_sdk_info())          # 同时给出 implemented / missing
    print(client.get_status())
    client.set_llm_config("planner", provider="deepseek", model="deepseek-chat")
    client.set_persona(name="Maid", personality="calm", speaking_style="brief")
    client.push_external_context({"system": "upstream persona engine", "mood": "neutral"})
    print(client.send_chat("一起去挖铁吧", sender="launcher"))
```

`python_client.py` 里其余方法（`list_skills`、`acquire_control`、`run_task_preset`、`list_runs`、`list_events`、
`list_schedules`、`get_identity`、`get_body_request`、`list_task_presets` …）对应的是**尚未实现**的 V2 面，
调用会拿到 404/错误响应。用 `get_sdk_info()["missing"]` 判断当前可用的子集，而不是照方法名写调用链。

本地开发可通过 `pip install -e backend/sdk` 安装 Python SDK。

## Browser / Electron

```js
import { LCUClient } from './backend/sdk/browser_client.js';

const client = new LCUClient('http://127.0.0.1:8080', { apiToken: '' });
await client.setLLMConfig('planner', { provider: 'openrouter', model: 'openai/gpt-4o-mini' });
await client.pushExternalContext({ source: 'launcher-ui', profileId: 'maid-alpha' });
const reply = await client.sendChat('跟我来', 'launcher-ui');
```

浏览器客户端与 Python 客户端是 1:1 的方法集合，同样只对上面「真实存在」的那批端点有效。

## Agent naming convention

当前实际的 agent key（`backend/agent/config_store.py:34-40` 的 `AGENT_ROLES`）：

- `default`（兜底 / 默认）
- `planner`
- `decider`
- `vision`
- `embedding`

默认开启单模型模式（`llm.single_model = true`）：五个角色先共用 `default` 一个模型。
其他名字可以存进配置，但只有被运行时显式调用才会生效。

（上一版这里写的 `timing_gate` / `self_prompter` 不是 LLM 角色：`timing_gate` 在本仓库**不存在**。）

## Actuator 注意事项

`send_command` / `/api/sdk/command` 会绕过自然语言 Planner，直接控制当前 Minecraft 客户端身体。它适合可信自动化、测试和直播控制器，不应暴露给不可信网页或公网调用方。它还会拒绝保留命令（`control_external` / `control_builtin` / `toggle_ai` / `shutdown`）。

`send_command` 返回 `request_id`。**但 26.3 没有回查端点**：`GET /api/v2/body-requests/{request_id}` 未注册，
所以拿到 ID 之后只能靠状态/任务面观察结果，不能轮询终态。

SDK 源码位于 `backend/sdk/`，独立采用 Apache License 2.0；Python 人格、记忆和规划后端仍采用 AGPL-3.0。

## 记忆范围（现状）

记忆按 **服务器 / 存档 / 维度** 分区持久化（`backend/agent/memory.py` 的 `WorldPoiMemory.use_namespace`，
`memory.py:344`），避免跨服、跨档、跨维度串数据；对话记忆走 `agent/message_db.py`。

**注意**：`GET /api/sdk/identity` / `POST /api/sdk/identity` 以及 `COMPANION_ID` / `MEMORY_SCOPE` /
`SERVER_ID` / `WORLD_ID` 环境变量在本仓库**不存在**（`agent/identity.py` 存在但只在不在活路径的
`memory_catalog.py` 里被 import）。上一版那一节描述的身份/记忆范围 SDK 属未实现设计。
