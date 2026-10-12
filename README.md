# LCU Mod - Minecraft AI Companion Client

LCU 是运行在独立真实 Minecraft 客户端上的 AI 陪玩平台。AI 使用一个实际登录游戏的客户端账号，拥有渲染窗口和第一人称主视角，可以在多人服务器中聊天、行动、直播和长期维持自己的角色身份。

项目同时提供可独立运行的人格、记忆与规划后端，以及面向第三方系统的 SDK。外部系统既可以向同伴注入人设和上下文，也可以在明确授权后读取状态或驱动游戏动作。

> [!NOTE]
> **平台与运行形态（本仓库实况，26.3 线）**
> - 平台是 **Minecraft 26.3 / NeoForge 26.3.0.57-beta**（`gradle.properties:11,17`）。模组声明的依赖区间下限是**真机验证过的** `26.3.0.42-beta`（`gradle.properties:23,25`）；两者不一致是**有意**的，`verifyMixin` 会拦住"悄悄放宽到一个没人跑过的版本"。
> - Java 工具链是 **25**（`build.gradle:38`），Mixin `compatibilityLevel=JAVA_25`（`src/main/resources/lcumod.mixins.json:5`）。CI 用 Temurin 25。
> - Java 侧是**纯客户端**模组：`LCUMod.commonSetup` 在非 `Dist.CLIENT` 时直接返回（`LCUMod.java:58-61`），**没有服务端/假玩家形态**。同一个 JAR 只有一种运行方式 —— 真实有头客户端 + WireServer + `ActionExecutor`。
> - **不存在** `runtimeRole` / `body_client` / `player_client` / `server_fake_player` / `fakePlayerEnabled` 这些键。也**没有**游戏内 `P` 键对话界面：全模组唯一的 `KeyMapping` 是 `F12`（`key.lcumod.toggle_ai`，`AIKeyHandler.java:31-36`）。

<details>
<summary>Java 侧全部配置键（`ModConfig.java`，共 7 个）</summary>

| 键 | 类型 | 默认值 | 位置 |
|---|---|---|---|
| `wirePort` | int（1024–65535） | `25568` | `ModConfig.java:9-11` |
| `wireBindAddress` | string | `"127.0.0.1"` | `ModConfig.java:16-19` |
| `wireToken` | string | `""`（空 = 不鉴权；非空则后端首帧必须 `hello`） | `LCUMod.java:69-81` |
| `stateInterval` | int（tick，1–200） | `10`（= 0.5 秒） | `ModConfig.java:30-32` |
| `collectSurroundings` | bool | `true` | `ModConfig.java:35-37` |
| `surroundingsRadius` | int（2–32） | `8` | `ModConfig.java:40-42` |
| `autonomousFallback` | bool | `false` | `ModConfig.java:46-51` |

</details>

> [!IMPORTANT]
> **存储与"生产就绪"的规则目前没有接线。** "生产必须 PostgreSQL、不得静默回退 SQLite" 这条规则写在 `backend/agent/storage_policy.py:22-48`（读 `LCU_ENV` / `LCU_STORAGE_BACKEND` / `DATABASE_URL`，production + 非 PostgreSQL 一律 `RuntimeError`，PostgreSQL 适配器未完成前 production 始终被拒），并有单测 `backend/tests/test_storage_policy.py` 覆盖。但**当前没有任何启动路径调用它**（`grep enforce_storage_policy` 只命中该文件与测试），所以 `LCU_ENV=production` 今天不会在启动时被拒。这是一条**设计约束，尚未接线**，不是已验证的运行时行为。

## 部署结论

**使用真实客户端 AI 时，LCU 只需要安装在 AI 使用的那个 Minecraft 客户端中，目标服务器不需要安装 LCU。** 普通玩家也不需要安装 —— 本仓库的模组没有玩家侧对话界面。

| 项 | 结论 |
|---|---|
| LCU 安装位置 | AI 使用的那一个真实 Minecraft 客户端 |
| 目标服务器是否安装 LCU | 否 |
| 是否区分运行角色 | 否 —— 只有一种客户端形态（`LCUMod.java:58-61`） |
| Java 侧对外提供什么 | WireServer（TCP JSONL，默认 `127.0.0.1:25568`）+ `ActionExecutor` 动作面 |

模组不注册自定义物品、方块、实体或必须由服务器接收的 LCU 网络包，因此不会要求对端安装同一模组。`ActionExecutor` 通过正常 Minecraft 客户端协议行动，受服务器权限、反作弊、距离、区块加载和菜单规则约束。协议有效不等于服务器允许自动化，也不保证不会被拒绝或踢出。

## 项目结构

```
lcumod/
├── src/                    # Minecraft 模组 (NeoForge, Java 25, 纯客户端)
│   └── main/java/com/lcu/lcumod/
│       ├── action/         # ActionExecutor、寻路、反射、容器控制、POI 记忆
│       ├── state/          # StateCollector（状态与 near 收集）
│       ├── network/        # WireServer / WireAuth / ControlStream 入口
│       ├── client/         # 客户端 HUD/输入隔离/窗口标题/界面守卫
│       └── proto/          # 协议常量（PROTOCOL_VERSION = 1）
├── backend/                # AI 大脑与集成后端 (Python, FastAPI)
│   ├── agent/              # 会话、规划器、模式引擎、记忆、权限、命令闸门
│   ├── protocol/           # WireClient（与 Java WireServer 通信）
│   ├── api/                # 增量挂载的 SDK/V2 面与 overlay 数据面
│   ├── sdk/                # Python / Browser SDK（Apache-2.0）
│   └── web/                # 本地控制台（5 个静态页）
├── overlay/                # OBS 直播叠层素材 + 渲染/自检脚本
└── docs/                   # 文档
```

## 核心能力

### 真实客户端身体
- 以真实 Minecraft 客户端账号加入单人或多人游戏
- 保留渲染窗口和第一人称主视角，适合直播与录像
- Java 模组负责感知、寻路、输入隔离和动作执行；Python 后端负责人格、记忆、聊天、规划和外部集成
- AI 持控制权时**物理键盘不起作用**（只留 `F12` 切控制权、`ESC` 作安全出口；开着界面时按键归界面），避免人与 AI 同时写同一套操作

### 动作面（`ActionExecutor` 的命令分发，`ActionExecutor.java:512-668`）
- 移动与视角：`move_to`、`look_at`、`set_control_state`、`jump`、`sneak`、`sprint`、`stop_all`
- 世界交互：`mine_block`/`dig_block`、`place_block`、`interact_block`/`interact`、`use_on`、`use_on_entity`、`attack_entity`、`attack`
- 背包与容器：`get_inventory`、`select_hotbar`、`equip`/`auto_equip`/`equip_armor`/`equip_weapon`、`drop`/`drop_item`、`sort_inventory`、`get_container`、`take_item`、`put_item`、`container_move`、`container_click`、`close_container`
- 生存与生活：`eat`、`sleep`、`trade`、`mlg`（落地水）、`craft_item`、`craft_via_recipe`、`search_recipes`、`collect_blocks`、`build`
- 导航与站点：`find_block`、`go_to_block`、`explore`、`follow_player`、`come_to_player`、`route_record_start`/`route_record_stop`/`route_follow`/`remember_route`/`route_export`、`remember_poi`、`remember_station`
- 反射与防线：`CombatReflex` / `SwimReflex` / `MlgReflex` / `GearReflex` 作为每 tick 的**最后写者**，可覆盖控制流；`reflex_override` 控制抑制窗口
- 控制权：`toggle_ai` 对应游戏内 `F12`

### 后端任务与对话
- 人格（`persona_store`）、按服务器/世界隔离的记忆（`memory` / `world_memory` / `person_memory`）、联系人（`contacts`）
- 分层回复门（`permissions.PermissionsStore` + `ReplyGate`）与指令闸门（`command_gate.CommandGate`，硬黑名单 kill/kick/ban/op/stop/gamerule… 不可白名单）
- 任务列表（`task_list`）、作业注册表（`job_registry`）、资源租约（`resource_lease`）
- 上下文预算（`context_budget`）：`context_window` / `reserve_output` / `compress_at` / `hysteresis` / `keep_recent`

### 记忆与「家」

记忆点都带**标签**，一个位置可以同时挂多个标签 —— 同一张床既是 `bed` 也是 `home`。状态里以 `remembered_pois` 发给后端（`StateCollector.java:617`），每条带 `category`（主标签）和 `tags`（全部标签）。

- **主动记，不用人类提醒**：进世界后它自己记住原版**重生点**；第一次在附近**看到床**，就把那个位置记成 `bed` + `home`。
- **记下床位 ≠ 家绑上了**：原版要在这张床上**右键一次**才会把重生点绑到它（任何时间都行；"现在不能睡"只是不睡，不影响绑定）。所以它会自己走过去右键一次，并用「原版当前重生点 == 这张床」核对（`state_update.home_bed_bound`，`StateCollector.java:397-441`）；右键两次仍不是这张床 ⇒ 如实报失败。
- **床没了 ⇒ 作废「床」标签，不作废「家」**：判据是**那个位置已经不是床方块了**（区块没加载就不下结论）。`home_bed_bound=false` **只代表没绑上**，**不代表床没了**（`backend/agent/home_bed.py:115`）。
- **只记事实，不做主**：模组只把这些带标签的记忆如实发到后端；要不要回去、要不要做床由模型自己决定 —— 后台**没有自主开关**：`modes_engine.add_defaults` 只装 P0–P3 四个生存反射（`modes_engine.py:645-656`），原来的自动进食/收集/狩猎类模式已删除。

## SDK 与集成

SDK 分为三组接口：

- **Gateway**：把外部消息、人设和上下文送入同伴自己的决策链
- **Observer**：读取会话、状态、记忆和配置
- **Actuator**：在授权后直接向当前客户端身体发送低层动作

后端默认只监听 `127.0.0.1`。对局域网或远程地址开放**必须**设置 `SDK_API_TOKEN`，否则启动直接报错（`main.py:29-39`）；浏览器跨域接入还需设置 `SDK_ALLOWED_ORIGINS`。当前 SDK 自报 `auth_required=false`（26.3 没有 SDK token 中间件）。真实可用的端点清单与已知缺口见 [SDK 文档](docs/sdk.md)。

同伴拥有跨重启稳定身份，记忆可配置为全局、按服务器或按世界隔离。

## 许可证

整个项目以 **AGPL-3.0** 发布（与构建元数据 `gradle.properties` 的 `mod_license=AGPL-3.0` 一致）：

| 部分 | 协议 | 说明 |
|------|------|------|
| Minecraft 客户端模组 (`src/`) | **AGPL-3.0** | 与 `gradle.properties:35` 的 `mod_license` 一致 |
| Python 后端 (`backend/`，不含 `sdk/`) | **AGPL-3.0** | 人格、记忆、规划和服务端实现 |
| 集成 SDK (`backend/sdk/`) | **Apache-2.0**（有意保留的例外） | 面向第三方 Python / Browser / Electron 集成；协议层保持宽松，避免 AGPL 传染到接入方的程序 |

非本项目代码：`TEMPLATE_LICENSE.txt` 是 NeoForge MDK 模板自带的声明（MIT，属模板本身）。

详见：
- [AGPL-3.0](LICENSE) — 项目根与模组
- [AGPL-3.0](backend/LICENSE) — 后端
- [Apache-2.0](backend/sdk/LICENSE) — SDK（唯一例外，见上）

## 快速开始

### 前置要求
- **Java 25**（工具链固定为 25，`build.gradle:38`）
- **Python 3.11+**
- **Minecraft 26.3** —— 构建与 API 实证用 NeoForge **26.3.0.57-beta**（`neo_version`）；模组声明的依赖区间下限是 `neo_version_validated`（当前 `26.3.0.42-beta`）

### 构建模组
```bash
.\gradlew.bat build
```

构建产物位于 `build/libs/lcumod-0.1.0.jar`。`build` 会跑 `check`，而 `check` 在这一版接的是 **18 个零依赖 `verifyXxx` 自检**（不是 JUnit —— 26.3 已去掉 JUnit，`build.gradle:389-393` 把默认 `test` 任务设为允许零用例）。

### 启动后端
```bash
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

默认地址为 `http://127.0.0.1:8080`。不要在未配置 `SDK_API_TOKEN` 时监听非回环地址。

网页控制台有 5 个页面：`/`（控制台）、`/contacts`、`/overlay`（整屏舞台）、`/overlay/editor`、`/overlay/{component}`（OBS 组件单页，见 [overlay/README.md](overlay/README.md)）。

### 后端热载（改完 Python 不用重启）

改完后端 Python 逻辑后**不用重启进程**：进程内 `importlib.reload`，**不掉 wire 连接**、不丢内存态（连接着游戏时尤其重要）。`uvicorn --reload` 是重启进程 —— 会掉线并清空运行期状态，本仓**不用**它。

- `POST /api/reload` —— 立刻热载一次。空闲时回 `{"ok": true, ...}` + 报告：`reloaded`（真重载了的模块）、`failed`（重载抛错、已跳过继续的）、`needs_restart`（**改了要重启**的那些，逐条带原因）；同时用会话**已有**的构件重建 `session.planner`（不新造 wire / 记忆）。
- `POST /api/reload/watch {"enabled": true|false}` —— 开关后台看门狗：按间隔轮询源文件快照，**只在空闲时**自动热载。开机即开用环境变量 `LCU_HOT_RELOAD=1`（间隔 `LCU_HOT_RELOAD_INTERVAL`，默认 2 秒）。
- **忙时一律拒绝 / 跳过**：会话有**未终态**的 job、`task_state` 不是 idle/终态、或 planner 上有**挂起的复合体**就算忙 —— `/api/reload` 回 `{"ok": false, "reason": "busy", "busy": "…"}`（**绝不热一半**），看门狗则**跳过 + 打日志**，把这次变化挂起，等空闲再一次性热载（不排队堆叠）。

**能热载（白名单，`agent/hot_reload.py` 的 `HOT_MODULES`，按依赖顺序）**：`planner`、`perception`、`entities`、`farm`、`modes_engine`、`territory`、`use_on`、`anchor`、`context_budget`、`composites`、`home_bed`、`skills`、`tool_registry`、`craft`、`borrow`、`haul`、`where`、`container_view`。

**不能热载（改了要重启，`needs_restart` 会点名）**：`session`、`orchestrator`、`llm_service`、`memory`、`message_db`、`config_store`、`state_governor`、`action_manager`、`protocol`（wire 连接：热载＝断线）、`server`、`api/*`，以及**白名单之外**的任何模块（拿不准就不热载）。

两条诚实边界：① `importlib.reload` 是**原地**重载 —— **已经创建**的实例仍是旧类对象，所以白名单里那几个"活实例握在会话手里"的模块（`skills` / `modes_engine` / `territory` / `borrow`）改了，模块换了新代码、但那个实例要**重启**才换，报告里同样点名；② `session` 里早先 `from x import f` 拿到的老绑定也不会跟着变。因此热载对"重载后重新 import 它们的人"（planner 及白名单内部）立即生效，会话层那种老绑定要重启。

### 配置
后端配置文件位于 `backend/.local/config.json`（自动生成，已加入 .gitignore）。

管理台保存的模型 URL 和 API Key 都会写入这个文件。API Key 在 REST 响应和管理台中会脱敏为 `***`，但本机文件当前是明文 JSON，并非加密密钥库。不要手动强制添加 `backend/.local/`、`backend/config.json`、`backend/.env` 或 `backend/.api_key`；这些路径已由仓库的 `.gitignore` 排除。提交前仍应执行密钥扫描，并把生产密钥交给操作系统密钥库或独立 secrets manager。

支持的 LLM 提供商预设（`config_store.py:44-140`，共 9 个）：
- **DeepSeek（默认）** —— `_default_config()` 明确取 DeepSeek 的 OpenAI 兼容端点（`config_store.py:223`）
- OpenAI、MiMo、OpenRouter、Qwen、Kimi、SiliconFlow、Ollama、Custom

`AGENT_ROLES` 目前是 `default` / `planner` / `decider` / `vision` / `embedding` 五个角色；默认开启单模型模式（`llm.single_model = true`），全部角色先用 `default` 一个模型。

### 估算与假设（**未经证实，勿当事实引用**）

- **上下文窗口不是事实，是配置假设**：`context_budget.context_window` 默认 **128000** 只是本地默认值（`backend/agent/context_budget.py:226`）。provider 的模型列表**不返回窗口大小**，仓库里也没有对任何模型真实窗口的验证。`reserve_output=16384`、`compress_at=0.70`、`keep_recent=6`、`hysteresis=0.10` 同样是本地默认值，不是实测结果。
- 默认预设模型名 `deepseek-v4.1-flash` 只是 `config_store.py:73-85` 里的一个字符串；能否真调通取决于你填的 API Key 与端点。
- `storage_policy` 的"生产必须 PostgreSQL"是**代码里的规则**，当前**未接线**（见上文 IMPORTANT）。
- 模组侧 `wireToken` **是接线了的**：启动时（`WIRE.start()` 之前）把配置值交给 `WireServer.setRequiredToken`，并打日志（武装了报位数、**空值打 WARN**）。空 = 不鉴权（默认；端口只监听回环）。要让鉴权生效，**两边设成同一个值**：模组 `config/lcumod-local.toml` 的 `wireToken` + 后端 `LCU_WIRE_TOKEN`（或 `.local/wire_token` 文件）。
- `overlay/` 的几何是渲染源码里的数值（`overlay/render.py:26-31`）；本文档只是把它们与 PNG 对齐，未逐像素核对渲染结果。
- `docs/vanilla-capability-matrix.md` 的覆盖矩阵状态是按 `ActionExecutor` 分发表与现有自检**推导出的判断**，不是逐场景真机验收记录。
- Java 侧 `verifyXxx` 自检跑在无游戏客户端的进程里（会加载真实 MC 26.3 类，但**没有游戏循环**），所以"分带头验收"仍需人工在有头客户端上完成。

## 开发

自主游玩、原版能力矩阵与单人通关验收路线见 [Autonomous Companion Roadmap](docs/roadmap.md)。

SDK 端点现状、真实可用的接口面与已知缺口见 [SDK 文档](docs/sdk.md)。

状态同步、模型上下文、多 Agent 通讯、动态工具发现和视觉 UI 操作契约见
[运行时编排设计](docs/runtime-orchestration.md)（**设计稿**：文中标注"计划/设计"的部分尚未实现，以代码为准）。

Java 能力清单与覆盖矩阵见 [原版能力矩阵](docs/vanilla-capability-matrix.md)。

### 测试
```bash
cd backend
pip install -r requirements.txt
python -m unittest discover -s tests
```
2026-10-12 实测：**1733 tests OK（零 skip）**。Java 侧零依赖自检是 `check` 里的 **18 个 `verifyXxx` 任务**（共 **1307 项**）；逐项与完整验收步骤见 [TEST_TABLE.md](TEST_TABLE.md)。

### 代码结构
- `ActionExecutor.java` — Java 端动作执行中枢（命令分发 + 各 handler）
- `CraftingPlanner.java` — 递归配方分析
- `PoiMemory.java` — 带标签的位置记忆（工作站/仓库/床/home/重生点）
- `session.py` — 后端会话管理（活路径的模块装配点）
- `planner.py` — LLM 规划器
- `modes_engine.py` — 生存反射模式引擎（P0–P3）

## 致谢

设计上参考过的项目（下列特征在 26.3 中未必都存在）：
- [mineflayer](https://github.com/PrismarineJS/mineflayer) — 控制状态与物理
- [mindcraft](https://github.com/kolbytn/mindcraft) — 动作管理器
- [MaiBot](https://github.com/MaiBot-family/MaiBot) — 回复节奏
- [TouhouLittleMaid](https://github.com/TartaricAcid/TouhouLittleMaid) — Brain/Activity 架构
