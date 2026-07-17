# LCU Companion Integration SDK

LCU SDK 让外部系统接入一个正在运行的 Minecraft AI 同伴，而不是绕过人格和记忆创建另一套机器人。

接口分为：

- `Gateway`：发送聊天、注入 persona 和外部上下文，由同伴自己的 Planner 决策
- `Observer`：读取状态、会话、记忆、配置和 SDK 能力
- `Actuator`：直接发送低层游戏命令，仅供可信控制器使用

SDK API 版本可通过 `GET /api/sdk/info` 查询，当前为 `1`。

V2 基础接口增加可发现的 Skill Registry 和排他控制租约。V1 保持兼容；新上游 Agent 应优先使用 V2。

## 安全模型

后端默认监听 `127.0.0.1:8080`。如果通过 `WEB_HOST=0.0.0.0` 或其他非回环地址开放，必须设置强随机 `SDK_API_TOKEN`。

Java 模组的 wire server 固定只监听 `127.0.0.1`。生产环境仍应在 NeoForge 的 `lcumod` 配置中设置 `wireToken`，并把同一值写入后端 `MOD_WIRE_TOKEN`；新连接通过认证前不能替换当前后端连接。wire token 与 SDK bearer token 是两个独立凭据。

建议使用 URL-safe token，例如：`python -c "import secrets; print(secrets.token_urlsafe(32))"`。

REST 客户端使用：

```http
Authorization: Bearer <SDK_API_TOKEN>
```

WebSocket 的非浏览器客户端使用 `Authorization` 请求头；浏览器控制台通过 `lcu-token.<SDK_API_TOKEN>` 子协议认证。浏览器跨域调用必须把完整 origin 配置到 `SDK_ALLOWED_ORIGINS`，多个 origin 使用逗号分隔。

需要从浏览器打开受保护的内置控制台时，使用 URL fragment：`http://127.0.0.1:8080/#token=<SDK_API_TOKEN>`。Fragment 不会发送到 HTTP 服务器或写入访问日志。

## REST API

- `GET /api/llm/providers` — 返回服务商预设
- `POST /api/llm/config` — 保存某个 agent 的 LLM 配置
- `POST /api/llm/models` — 按当前配置远程拉取模型列表
- `GET /api/persona` — 读取当前人设
- `POST /api/persona` — 更新默认人设
- `GET /api/sdk/context` — 读取外部注入上下文
- `POST /api/sdk/context` — 写入外部注入上下文
- `GET /api/status` — 读取后端状态
- `GET /api/session` — 读取当前会话状态
- `GET /api/memory` — 读取当前记忆摘要、玩家关系、服务器经历和最近任务结果
- `GET /api/config` — 读取运行配置
- `GET /api/sdk/info` — 读取 SDK 版本与接口能力
- `GET /api/sdk/identity` — 读取稳定同伴 ID 与当前记忆范围
- `POST /api/sdk/identity` — 修改身份或记忆范围，重启后生效
- `POST /api/sdk/chat` — 通过人格、记忆和 Planner 发送消息
- `POST /api/sdk/command` — 直接向已连接的客户端身体发送动作
- `GET /api/v2/info` — 查询 V2 控制模式与能力
- `GET /api/v2/skills` — 查询带输入 schema 的 Skill 清单
- `GET /api/v2/skills/{id}` — 查询单个 Skill manifest
- `POST /api/v2/skills/{id}/runs` — 校验输入并执行 Skill
- `GET /api/v2/control` — 查询当前控制模式和活动租约
- `POST /api/v2/control/leases` — 申请 `external` 控制租约
- `POST /api/v2/control/leases/{id}/heartbeat` — 续租
- `POST /api/v2/control/leases/{id}/release` — 释放控制权

## Python

```python
from sdk import LCUClient

with LCUClient("http://127.0.0.1:8080", api_token="optional-local-token") as client:
    print(client.get_sdk_info())
    client.set_llm_config("planner", provider="deepseek", model="deepseek-chat")
    client.set_persona(name="Maid", personality="calm", speaking_style="brief")
    client.push_external_context({"system": "upstream persona engine", "mood": "neutral"})
    response = client.send_chat("一起去挖铁吧", sender="launcher")
    print(response)
```

完整上游 Agent 使用排他租约：

```python
with LCUClient("http://127.0.0.1:8080", api_token="token") as client:
    lease = client.acquire_control("roleplay-agent", mode="external", ttl_seconds=30)
    try:
        skills = client.list_skills("general")
        run = client.run_skill(
            "general.craft_item",
            {"item": "minecraft:iron_pickaxe", "count": 1},
            lease_id=lease["id"],
            fencing_token=lease["fencing_token"],
        )
        print(run)
    finally:
        client.release_control(lease["id"], lease["fencing_token"])
```

`external` 模式接管 `persona`、`memory`、`planner`、`autonomy` 和 `actions` 全部控制域。租约有效时，本地 Planner、Python 自主模式、Java 自主战斗/漫游、拟人空闲动作和 anti-AFK 均停用；断线停止和自动重生仍保留为身体安全能力。调用方必须在租约到期前发送 heartbeat，租约到期后运行时自动回到 `builtin`。

`backend/.local/agent_state.db` 使用迁移化 SQLite 保存 Skill 元数据和控制租约；可执行 Skill 代码不存入数据库。当前 V2 Skill run 返回底层 request ID，持久化任务运行、进度查询、事件游标和定时调度将在后续 V2 资源中扩展。

租约响应中的 `runtime_status` 为 `queued` 或 `pending_connection`。它表示控制转换已提交到身体或正在等待身体重连，不代表 Java 已返回应用层确认；调用方在开始动作前应持续 heartbeat，并通过后续状态事件确认控制模式。本阶段将在事件流协议完成后补充明确的 `applied` 状态。

本地开发可通过 `pip install -e backend/sdk` 安装 Python SDK。

## Browser / Electron

```js
import { LCUClient } from './backend/sdk/browser_client.js';

const client = new LCUClient('http://127.0.0.1:8080', { apiToken: '' });
await client.setLLMConfig('planner', { provider: 'openrouter', model: 'openai/gpt-4o-mini' });
await client.pushExternalContext({ source: 'launcher-ui', profileId: 'maid-alpha' });
const reply = await client.sendChat('跟我来', 'launcher-ui');
```

## Agent naming convention

当前主链实际使用的 agent key：

- `default`
- `planner`
- `timing_gate`
- `self_prompter`

其他 agent 名称可以保存配置，但只有被运行时显式调用后才会生效。

## Actuator 注意事项

`send_command` / `/api/sdk/command` 会绕过自然语言 Planner，直接控制当前 Minecraft 客户端身体。它适合可信自动化、测试和直播控制器，不应暴露给不可信网页或公网调用方。

SDK 源码位于 `backend/sdk/`，独立采用 Apache License 2.0；Python 人格、记忆和规划后端仍采用 AGPL-3.0。

## 身份与记忆范围

每个同伴首次运行时会生成稳定 `companion_id`，此 ID 不等于每次进程启动生成的 Session ID。记忆保存在 `backend/.local/companions/`，支持三种范围：

- `global`：同伴在所有服务器和世界共享记忆
- `server`：按 `server_id` 隔离
- `world`：按 `server_id + world_id` 隔离

当前服务器地址或单机存档还不能由协议可靠识别，因此 `server_id` 和 `world_id` 需要通过配置、SDK 或环境变量明确提供。修改身份范围后需要重启后端。

环境变量 `COMPANION_ID`、`MEMORY_SCOPE`、`SERVER_ID`、`WORLD_ID` 的优先级高于持久化配置；设置任一变量时，SDK 会拒绝修改身份，避免返回无法生效的配置。
