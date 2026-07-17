# LCU Mod - Minecraft AI Companion Client

LCU 是运行在独立真实 Minecraft 客户端上的 AI 陪玩平台。AI 使用一个实际登录游戏的客户端账号，拥有渲染窗口和第一人称主视角，可以在多人服务器中聊天、行动、直播和长期维持自己的角色身份。

项目同时提供可独立运行的人格、记忆与规划后端，以及面向第三方系统的 SDK。外部系统既可以向同伴注入人设和上下文，也可以在明确授权后读取状态或驱动游戏动作。

未来会增加服务端假玩家作为第二种可选身体，但真实有头客户端仍是直播和主视角场景的核心。

## 项目结构

```
lcumod/
├── src/                    # Minecraft 模组 (NeoForge, Java)
│   └── main/java/com/lcu/lcumod/
│       ├── action/         # 动作执行器、寻路、POI 记忆
│       ├── state/          # 状态收集器
│       └── network/        # 网络通信 (WireServer)
├── backend/                # AI 大脑与集成后端 (Python, FastAPI)
│   ├── agent/              # 会话管理、规划器、模式引擎
│   ├── protocol/           # 通信协议
│   ├── sdk/                # Python / Browser SDK
│   └── web/                # 本地控制台
└── docs/                   # 文档
```

## 核心能力

### 真实客户端身体
- 以真实 Minecraft 客户端账号加入单人或多人游戏
- 保留渲染窗口和第一人称主视角，适合直播与录像
- Java 模组负责感知、寻路、输入隔离和动作执行
- Python 后端负责人格、记忆、聊天、规划和外部集成

### 通用任务协议
- 递归配方分析（crafting + smelting + blasting + smoking）
- 统一任务状态机（craft / collect / follow / eat / stop）
- 自动依赖解析和子任务派发

### 资源采集链
- 地表掉落物拾取
- 方块挖掘（自动装备最佳工具）
- 仓库取物（记忆仓库位置和内容，优先从已知有目标物品的仓库取）

### 工作站管理
- 记忆附近工作站和仓库位置
- 自动放置工作站（从背包拿出并放置）
- 仓库内容缓存（开箱时记住里面有什么）

### 炉子加工链
- 自动燃料获取（从背包、仓库、地表）
- 炉子/高炉/烟熏炉状态管理
- 处理中不重复放 recipe

## SDK 与集成

SDK 分为三类接口：

- **Gateway**：把外部消息、人设和上下文送入同伴自己的决策链
- **Observer**：读取会话、状态、记忆和配置
- **Actuator**：在授权后直接向当前客户端身体发送低层动作

后端默认只监听 `127.0.0.1`。对局域网或远程地址开放时必须设置 `SDK_API_TOKEN`，浏览器跨域接入还需设置 `SDK_ALLOWED_ORIGINS`。详见 [SDK 文档](docs/sdk.md)。

同伴拥有跨重启稳定身份，记忆可配置为全局、按服务器或按世界隔离。运行期 Session ID 仅用于诊断，不再决定记忆文件位置。

## 许可证

本项目按模块采用不同许可证：

| 部分 | 协议 | 说明 |
|------|------|------|
| Minecraft 客户端模组 (`src/`) | **MIT** | 便于整合包、客户端和兼容项目采用 |
| Python 后端 (`backend/`，不含 `sdk/`) | **AGPL-3.0** | 人格、记忆、规划和服务端实现 |
| 集成 SDK (`backend/sdk/`) | **Apache-2.0** | 面向第三方 Python、Browser 和 Electron 集成 |

详见：
- [MIT License](LICENSE) — 模组部分
- [AGPL License](backend/LICENSE) — 后端部分
- [Apache-2.0 License](backend/sdk/LICENSE) — SDK 部分

## 快速开始

### 前置要求
- Java 21+
- Python 3.11+
- Minecraft 1.21.1 (NeoForge)

### 构建模组
```bash
.\gradlew.bat build
```

### 启动后端
```bash
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

默认地址为 `http://127.0.0.1:8080`。不要在未配置 `SDK_API_TOKEN` 时监听非回环地址。

### 配置
后端配置文件位于 `backend/.local/config.json`（自动生成，已加入 .gitignore）。

支持的 LLM 提供商预设：
- MIMO（默认）
- OpenAI
- DeepSeek
- OpenRouter
- Qwen
- Kimi
- SiliconFlow
- Ollama

## 开发

### 测试
```bash
cd backend
.venv\Scripts\python.exe -m unittest discover -s tests
```

### 代码结构
- `ActionExecutor.java` — Java 端动作执行中枢
- `CraftingPlanner.java` — 通用递归配方分析器
- `PoiMemory.java` — 工作站/仓库记忆系统
- `session.py` — 后端会话管理
- `planner.py` — LLM 规划器
- `modes_engine.py` — 自主行为模式引擎

## 致谢

参考项目：
- [mineflayer](https://github.com/PrismarineJS/mineflayer) — 控制状态系统
- [mindcraft](https://github.com/kolbytn/mindcraft) — 动作管理器
- [MaiBot](https://github.com/MaiBot-family/MaiBot) — Timing Gate 和两阶段推理
- [TouhouLittleMaid](https://github.com/TartaricAcid/TouhouLittleMaid) — Brain/Activity 架构
