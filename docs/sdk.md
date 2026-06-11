# LCU Mod Integration SDK

`LCU Mod` 现在提供一层稳定的上游集成接口，目标是让外部系统可以：

- 注入自己的 `persona` / 人设上下文
- 给不同 agent 配不同的服务商、URL、模型、温度、token 上限
- 动态拉取模型列表
- 读取运行状态和基础配置

## REST API

- `GET /api/llm/providers` — 返回服务商预设
- `POST /api/llm/config` — 保存某个 agent 的 LLM 配置
- `POST /api/llm/models` — 按当前配置远程拉取模型列表
- `GET /api/persona` — 读取当前人设
- `POST /api/persona` — 更新默认人设
- `GET /api/sdk/context` — 读取外部注入上下文
- `POST /api/sdk/context` — 写入外部注入上下文
- `GET /api/status` — 读取后端状态

## Python

```python
from sdk import LCUClient

client = LCUClient("http://127.0.0.1:8080")
client.set_llm_config("planner", provider="deepseek", model="deepseek-chat")
client.set_persona(name="Maid", personality="calm", speaking_style="brief")
client.push_external_context({"system": "upstream persona engine", "mood": "neutral"})
models = client.fetch_models("planner")
print(models)
client.close()
```

## Browser / Electron

```js
import { LCUClient } from './backend/sdk/browser_client.js';

const client = new LCUClient('http://127.0.0.1:8080');
await client.setLLMConfig('conversation', { provider: 'openrouter', model: 'openai/gpt-4o-mini' });
await client.pushExternalContext({ source: 'launcher-ui', profileId: 'maid-alpha' });
```

## Agent naming convention

当前支持的 agent key：

- `default`
- `planner`
- `timing_gate`
- `self_prompter`
- `conversation`

如果上游要新增专用 agent，可以直接向 `/api/llm/config` 传新的 `agent` 名称，后端会持久化保存。
