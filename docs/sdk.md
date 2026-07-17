# LCU Companion Integration SDK

LCU SDK 让外部系统接入一个正在运行的 Minecraft AI 同伴，而不是绕过人格和记忆创建另一套机器人。

接口分为：

- `Gateway`：发送聊天、注入 persona 和外部上下文，由同伴自己的 Planner 决策
- `Observer`：读取状态、会话、记忆、配置和 SDK 能力
- `Actuator`：直接发送低层游戏命令，仅供可信控制器使用

SDK API 版本可通过 `GET /api/sdk/info` 查询，当前为 `1`。

## 安全模型

后端默认监听 `127.0.0.1:8080`。如果通过 `WEB_HOST=0.0.0.0` 或其他非回环地址开放，必须设置强随机 `SDK_API_TOKEN`。

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
- `GET /api/memory` — 读取当前记忆摘要
- `GET /api/config` — 读取运行配置
- `GET /api/sdk/info` — 读取 SDK 版本与接口能力
- `GET /api/sdk/identity` — 读取稳定同伴 ID 与当前记忆范围
- `POST /api/sdk/identity` — 修改身份或记忆范围，重启后生效
- `POST /api/sdk/chat` — 通过人格、记忆和 Planner 发送消息
- `POST /api/sdk/command` — 直接向已连接的客户端身体发送动作

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
