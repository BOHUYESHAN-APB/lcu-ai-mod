# Autonomous Companion Roadmap

LCU 的目标不是让模型直接猜测每个客户端 tick，而是让模型负责目标和策略，稳定运行时负责感知、任务、Skill、验证、恢复与安全。

> **状态口径**：本文是**路线图**。每个 Gate 下的「现状（26.3）」只写代码里确实存在的东西；其余条目一律是**目标**，不是已完成项。上一版把 Gate 1/2 标成「已完成」，那是另一条血缘（master 1.21.1）的口径，本仓库不成立。

## Delivery Gates

### 1. Runtime Control

目标：

- 可发现、带 schema 的 Skill Registry
- 可续租且下沉到 Java 的排他控制权
- 真实客户端与未来身体共享 `BodyAdapter`
- SDK、wire 和浏览器边界具备独立认证

现状（26.3）：

- **Skill Registry 的 HTTP 面不存在**：`GET /api/v2/skills` 未注册（`backend/tests/test_http_surface.py:122-129` 断言为 missing）；`backend/agent/skill_registry.py` 文件在、不在活路径（没有任何活模块 import 它）。
- **没有"外部接管"控制模式**：`GET /api/v2/control` 恒返回 `mode="builtin"`、`coordinator=null`（`backend/api/http_surface.py:296-299`）；`POST /api/v2/control/leases*` 未注册。真实控制权在 Java 侧由 `InputIsolation` + `ControlStream` 持有，用游戏内 `F12` 切换。
- **wire 边界有可选鉴权（默认关）**：`wireToken` 会在启动时真的武装（`LCUMod.commonSetup` ⇒ `WireServer.setRequiredToken`，空值打 WARN 明说不鉴权）；默认空值 ⇒ 不鉴权，但端口只监听回环。要开鉴权，模组与后端必须用同一个 token（见 [README](../README.md) 的配置说明与 `TEST_TABLE.md` 注意事项 7）。
- **SDK 边界**：后端监听非回环地址时**强制**要求 `SDK_API_TOKEN`（`backend/main.py:29-39`），但 SDK 面自报 `auth_required=false` —— 26.3 没有 SDK token 中间件。

### 2. Durable Operations

目标：

- Task Run 使用独立 UUID，不依赖进程内 request ID
- response/progress 映射为持久化终态
- 严格递增、可恢复读取的事件游标
- 真实时间一次性/间隔日程
- 游戏 tick 间隔和游戏时段日程
- 重启、断线、时间跳变和 misfire 行为可预测

现状（26.3）：**未实现**。`/api/v2/runs`、`/api/v2/events`、`/api/v2/schedules` 全部未注册；`backend/agent/task_coordinator.py`、`task_preset_registry.py`、`decision_scheduler.py` 文件在、不在活路径。

### 3. Autonomous Director

目标：

- 根据人格、关系、世界进度和玩家活动生成中期目标
- 把目标分解为可验证 Task 图
- 同一时间只有一个主任务，生存事件和玩家指令可抢占
- 失败后诊断、恢复或重规划，不盲目重复
- 支持共同目标、主动邀请和适度社交频率预算

现状（26.3）：只有 4 个生存反射（P0 保命 / P1 回血 / P2 逃跑 / P3 还手，`backend/agent/modes_engine.py:645-656`）与后端任务列表（`agent/task_list.py`）；**没有**自主目标生成。

### 4. Vanilla 90%

目标 —— 按玩法能力族而非物品数量验收：

- 生存、移动、探索、资源采集
- 制作、熔炼、仓储和装备
- 建造、农业、动物、钓鱼
- 战斗、交易、附魔、铁砧、酿造
- 下界、要塞、末地和 Boss 进程
- 单人长期生存与多人合作

每个公开 Skill 必须有真实完成条件、失败条件和取消语义。占位聊天或只返回 accepted 的动作不计入覆盖率。

现状（26.3）：Java `ActionExecutor` 已有 74 个命令标签（`ActionExecutor.java:512-668`）；逐族覆盖状态见 [原版能力矩阵](vanilla-capability-matrix.md)。

### 5. Content Skills

目标：

- `core`：底层可信动作
- `general`：原版组合能力
- `content_adapter`：模组配方、机器、菜单和实体语义
- `workflow`：用户声明式任务流程

**设计（未实现）**：可执行包的版本化与签名目前在仓库里没有对应代码（无签名校验、无包分发路径），属目标而非现状。数据库只保存 registry 元数据、启用状态和 namespaced Skill state —— 同样属目标。SQLite 仅用于自动化测试和本地开发、生产环境必须使用 PostgreSQL：规则写在 `backend/agent/storage_policy.py`，但**当前没有接线**（见 [README](../README.md) 的存储说明）。

### 6. Operations UI

目标：

- 连接与控制权
- Skill 浏览和 schema 表单
- Task Run、进度、终态和取消
- 真实时间/游戏时间日程
- 人格、记忆和玩家关系
- 原版能力矩阵、诊断和事件日志

桌面与移动视图必须可操作，不能依赖直接输入任意 wire 命令完成常规工作流。

现状（26.3）：控制台只有 5 个静态页 —— `/`（连接状态、人格/LLM 配置、任务列表、联系人、数据库检索、工具/复合工具/治理）、`/contacts`、`/overlay`、`/overlay/editor`、`/overlay/{component}`。**没有** Skill / Task Run / 日程 / 记忆管理页面。

### 7. Single-player Acceptance

目标（综合验收）：

1. 新建原版生存世界并保持客户端有头运行。
2. 无玩家命令时建立食物、工具、避难所和安全库存。
3. 死亡、断线、缺料、路径失败和夜晚后能够恢复。
4. 稳定推进铁器、钻石、下界、要塞、末地和末影龙里程碑。
5. 连续运行期间所有动作都有 Task Run 和可查询终态。

注：第 5 条依赖 Gate 2 的持久化 Run，**当前不成立**。单人通关是最终综合验收，不替代各能力族的确定性测试。

各工作流的**当前**实现状态、SDK 端点现状、编排设计与 Java 能力覆盖分别见 [SDK 文档](sdk.md)、[运行时编排设计](runtime-orchestration.md) 与 [原版能力矩阵](vanilla-capability-matrix.md)；测试基线见 [TEST_TABLE.md](../TEST_TABLE.md)。
