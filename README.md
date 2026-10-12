# LCU Mod - Minecraft AI Companion Client

LCU 是运行在独立真实 Minecraft 客户端上的 AI 陪玩平台。AI 使用一个实际登录游戏的客户端账号，拥有渲染窗口和第一人称主视角，可以在多人服务器中聊天、行动、直播和长期维持自己的角色身份。

项目同时提供可独立运行的人格、记忆与规划后端，以及面向第三方系统的 SDK。外部系统既可以向同伴注入人设和上下文，也可以在明确授权后读取状态或驱动游戏动作。

> [!NOTE]
> **本分支平台与合并说明（`merge/master-into-26.3`，基于 26.3 线）**
> - 平台是 **Minecraft 26.3 / NeoForge 26.3.0.57-beta**（不是 1.21.1）。`src/**`、`gradle.properties`、`build.gradle`、`neoforge.mods.toml` 一律取 26.3 线。
> - Java 侧是 26.3 移植线的 AI 决策架构：控制流原语（`ControlStream`/`EdgeTrigger`/`ReflexGate`）、战斗/上浮反射（`CombatReflex`/`SwimReflex`）、`BlockFinder`、显式 UTF-8 线协议、HUD 描述词。
> - 后端（`backend/`）是两支的并集：26.3 的在场门、分层回复门（`permissions` + reply gate）、按服务器隔离的对话记忆、人设三段与任务列表，加上 master 线的 `access_policy` / `audit_log` / `body_registry` / `decision_scheduler` / `identity` / `skill_registry` / `task_coordinator` / `storage_policy` / `memory_*` / `world_model` 等模块。
> - 本文档下文（除本节外）来自 master 线，描述的是 1.21.1 线的目标设计。其中 **`runtimeRole`/`body_client`、`headlessMode`、玩家 `P` 键对话界面、body kernel、V2 控制租约的 Java 侧实现尚未移植到本分支**；master 的 `server.py` V2/SDK/player REST 端点也未合入（`server.py` 取 26.3 侧），管理台中这些页面当前会 404。这些内容属于待移植清单，见分支提交说明。

> [!IMPORTANT]
> SQLite 仅用于自动化测试和本地开发。生产部署必须使用 PostgreSQL；在 PostgreSQL 存储、迁移、并发和恢复验证完成前，当前后端不视为生产就绪，且不得静默回退到 SQLite。
> 运行环境通过 `LCU_ENV=test|development|production` 与 `LCU_STORAGE_BACKEND=sqlite|postgresql` 显式声明。当前 PostgreSQL 适配器尚未完成，因此 `LCU_ENV=production` 会拒绝启动，而不是降级使用 SQLite。

同一个 JAR 包含真实 AI 客户端、普通玩家对话客户端和未来服务端假玩家三种运行角色；
角色由运行位置和 `runtimeRole` 共同决定，不需要分别编译。真实有头客户端仍是直播和
主视角场景的核心。

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
- AI 持控制权时**物理键盘不起作用**（只留 `F12` 切控制权、`ESC` 作安全出口；开着界面时按键归界面），避免人与 AI 同时写同一套操作
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

### 记忆与「家」

记忆点都带**标签**，一个位置可以同时挂多个标签 —— 同一张床既是 `bed` 也是 `home`。标签决定这条记录**是什么**、以及它在什么条件下**作废**。状态里以 `remembered_pois` 发给后端，每条带 `category`（主标签）和 `tags`（全部标签）。

- **主动记，不用人类提醒**：进世界后它自己记住原版**重生点**（默认就是世界出生地）；第一次在附近**看到床**，就把那个位置记成 `bed` + `home`。也就是说，**它第一次找到的那张床就是家**；人类让它造新床时走的是同一条路 —— 床放在哪，家就在哪。一般情况下主动保存一次即可，后续可以变更。
- **记下床位 ≠ 家绑上了**：原版要在这张床上**右键一次**才会把重生点绑到它（**任何时间都行**，白天、晴天一样；"现在不能睡"只是不睡，不影响绑定）。所以它会自己走过去右键一次，并用「原版当前重生点 == 这张床」核对（`home_bed_bound`）；右键两次仍不是这张床 ⇒ 如实报失败，绝不假装绑上了。
- **家可以换**：人类明确要求设新床/造新床时，新的床位置会**替换**旧的 home，不会同时留下两个。
- **床没了 ⇒ 作废「床」标签，不作废「家」**：若某次复活它回到的是世界出生点而不是那张床（原版重生点已经不在那张床上），或那个位置已经不是床方块了，它会把这条记忆的 `bed` 标签**摘掉**，`home` 标签保留。它对外表达的仍是「家在 (x, y, z)，但床没了」；接下来该做的是**自己走回 home 的位置、重新找一张床或重新做一张床**，而不是把这个家忘掉。
- **只记事实，不做主**：模组只负责把这些带标签的记忆如实发到后端；要不要回去、要不要做床由模型自己决定（能力等于注册的工具，后台没有自主开关）。

## SDK 与集成

SDK 分为三类接口：

- **Gateway**：把外部消息、人设和上下文送入同伴自己的决策链
- **Observer**：读取会话、状态、记忆和配置
- **Actuator**：在授权后直接向当前客户端身体发送低层动作

后端默认只监听 `127.0.0.1`。对局域网或远程地址开放时必须设置 `SDK_API_TOKEN`，浏览器跨域接入还需设置 `SDK_ALLOWED_ORIGINS`。详见 [SDK 文档](docs/sdk.md)。

同伴拥有跨重启稳定身份，记忆可配置为全局、按服务器或按世界隔离。运行期 Session ID 仅用于诊断，不再决定记忆文件位置。

## 许可证

整个项目以 **AGPL-3.0** 发布（与构建元数据 `gradle.properties` 的 `mod_license=AGPL-3.0` 一致）：

| 部分 | 协议 | 说明 |
|------|------|------|
| Minecraft 客户端模组 (`src/`) | **AGPL-3.0** | 与 `gradle.properties` 的 `mod_license` 一致 |
| Python 后端 (`backend/`，不含 `sdk/`) | **AGPL-3.0** | 人格、记忆、规划和服务端实现 |
| 集成 SDK (`backend/sdk/`) | **Apache-2.0**（有意保留的例外） | 面向第三方 Python / Browser / Electron 集成；协议层保持宽松，避免 AGPL 传染到接入方的程序 |

非本项目代码：`TEMPLATE_LICENSE.txt` 是 NeoForge MDK 模板自带的声明（MIT，属模板本身）。

详见：
- [AGPL-3.0](LICENSE) — 项目根与模组
- [AGPL-3.0](backend/LICENSE) — 后端
- [Apache-2.0](backend/sdk/LICENSE) — SDK（唯一例外，见上）

## 快速开始

### 前置要求
- Java 21+
- Python 3.11+
- Minecraft 26.3 —— **构建与 API 实证**用 NeoForge **26.3.0.57-beta**（`gradle.properties` 的 `neo_version`）；模组声明的依赖区间下限是**真机验证过的**版本 `neo_version_validated`（当前 `26.3.0.42-beta`，大型整合包常见的就是它）。两者不一致是**有意**的，`verifyMixin` 会拦住"悄悄放宽到一个没人跑过的版本"；要抬下限就同步改这两个值

### 构建模组
```bash
.\gradlew.bat build
```

构建产物位于 `build/libs/lcumod-0.1.0.jar`。同一个文件用于三种角色。新配置默认是
真实 AI 客户端：

```toml
runtimeRole = "body_client"
```

普通玩家只需要对话窗口时，必须显式改为 `player_client`；专用服务端未来使用
`server_fake_player`。非法角色值会回退到不启动执行器的 `player_client`。

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
- `PoiMemory.java` — 带标签的位置记忆（工作站/仓库/床/home/重生点），含床丢失时的标签作废
- `session.py` — 后端会话管理
- `planner.py` — LLM 规划器
- `modes_engine.py` — 自主行为模式引擎

## 致谢

参考项目：
- [mineflayer](https://github.com/PrismarineJS/mineflayer) — 控制状态系统
- [mindcraft](https://github.com/kolbytn/mindcraft) — 动作管理器
- [MaiBot](https://github.com/MaiBot-family/MaiBot) — Timing Gate 和两阶段推理
- [TouhouLittleMaid](https://github.com/TartaricAcid/TouhouLittleMaid) — Brain/Activity 架构
