# LCU Mod - Minecraft AI Companion Client

LCU 是运行在独立真实 Minecraft 客户端上的 AI 陪玩平台。AI 使用一个实际登录游戏的客户端账号，拥有渲染窗口和第一人称主视角，可以在多人服务器中聊天、行动、直播和长期维持自己的角色身份。

项目同时提供可独立运行的人格、记忆与规划后端，以及面向第三方系统的 SDK。外部系统既可以向同伴注入人设和上下文，也可以在明确授权后读取状态或驱动游戏动作。

> [!IMPORTANT]
> SQLite 仅用于自动化测试和本地开发。生产部署必须使用 PostgreSQL；在 PostgreSQL 存储、迁移、并发和恢复验证完成前，当前后端不视为生产就绪，且不得静默回退到 SQLite。
> 运行环境通过 `LCU_ENV=test|development|production` 与 `LCU_STORAGE_BACKEND=sqlite|postgresql` 显式声明。当前 PostgreSQL 适配器尚未完成，因此 `LCU_ENV=production` 会拒绝启动，而不是降级使用 SQLite。

未来会增加服务端假玩家作为第二种可选身体，但真实有头客户端仍是直播和主视角场景的核心。

## 部署结论

**使用真实客户端 AI 时，LCU 只需要安装在 AI 使用的那个 Minecraft 客户端中，目标服务器不需要安装 LCU。** 普通玩家也不需要安装；只有想使用游戏内 `P` 键对话界面的玩家才需要在自己的客户端安装。

| 运行角色 | 安装位置 | 目标服务器需要 LCU | 当前状态 |
|----------|----------|--------------------|----------|
| `body_client` | AI 实体客户端 | 否 | 可测试，提供感知、动作、F12 控制和 WireServer |
| `player_client` | 普通玩家客户端 | 否 | 可测试，仅提供受限 AI 对话，不提供身体控制 |
| `server_fake_player` | Minecraft 服务端 | 是 | 仅有安全配置骨架，假玩家执行器尚未实现 |

`body_client` 通过正常 Minecraft 客户端协议行动，受服务器权限、反作弊、距离、区块加载和菜单规则约束。协议有效不等于服务器允许自动化，也不保证不会被拒绝或踢出。移动、世界修改、库存、战斗、聊天、自主行为、anti-AFK、后台运行和广域感知均有独立策略开关；高风险类别默认关闭。完整边界见 [服务器策略与反作弊安全](docs/server-policy-safety.md)。

当前模组不注册自定义物品、方块、实体或必须由服务器接收的 LCU 网络包，因此不会要求对端安装同一模组。服务器或整合包自身仍可能实施“客户端模组白名单”或固定模组列表，这属于目标服务器策略。完整部署方式与边界见 [部署与运行角色](docs/deployment.md)。

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
- 后端通过 `BodyAdapter` 接收统一事件和发送动作，未来假玩家身体无需分叉人格与记忆链

### 通用任务协议
- 递归配方分析（crafting + smelting + blasting + smoking）
- 统一任务状态机（craft / collect / follow / eat / stop）
- 自动依赖解析和子任务派发
- Task Run、执行进度和终态持久化，断线后不盲目重放
- 支持真实时间、游戏 tick 和游戏时段日程

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

构建产物位于 `build/libs/lcumod-0.1.0.jar`。AI 实体客户端需要在首次启动后将 `config/lcumod-common.toml` 中的角色显式设为：

```toml
runtimeRole = "body_client"
```

默认值是更安全的 `player_client`；默认角色不会启动动作执行器、F12 身体控制或 WireServer。

### 启动后端
```bash
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

默认地址为 `http://127.0.0.1:8080`。不要在未配置 `SDK_API_TOKEN` 时监听非回环地址。

网页管理台提供连接状态、人格/LLM 配置以及 V2 控制租约、Skill、Task Run、事件和真实/游戏时间日程管理。

### 配置
后端配置文件位于 `backend/.local/config.json`（自动生成，已加入 .gitignore）。

管理台保存的模型 URL 和 API Key 都会写入这个文件。API Key 在 REST
响应和管理台中会脱敏为 `***`，但本机文件当前是明文 JSON，并非加密密钥库。
不要手动强制添加 `backend/.local/`、`backend/config.json`、`backend/.env`
或 `backend/.api_key`；这些路径已由仓库的 `.gitignore` 排除。提交前仍应执行
密钥扫描，并把生产密钥交给操作系统密钥库或独立 secrets manager。

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

自主游玩、原版 90% 能力矩阵、用户 Skill 与单人通关验收路线见 [Autonomous Companion Roadmap](docs/roadmap.md)。

当前需求、架构决策、未决问题、实施顺序和发布阻塞项统一记录在 [LCU Project Tracker](docs/project-tracker.md)。长期开发过程中应先更新该文件，避免对话上下文压缩造成需求丢失。

当前整合包的 WATUT、墓碑、背包整理等适配状态见 [模组兼容矩阵](docs/mod-compatibility.md)。

状态同步、模型上下文、多 Agent 通讯、动态工具发现和视觉 UI 操作契约见
[运行时编排设计](docs/runtime-orchestration.md)。

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
