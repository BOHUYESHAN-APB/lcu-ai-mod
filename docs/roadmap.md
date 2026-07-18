# Autonomous Companion Roadmap

LCU 的目标不是让模型直接猜测每个客户端 tick，而是让模型负责目标和策略，稳定运行时负责感知、任务、Skill、验证、恢复与安全。

## Delivery Gates

### 1. Runtime Control

- 可发现、带 schema 的 Skill Registry
- 可续租且下沉到 Java 的排他控制权
- 真实客户端与未来身体共享 `BodyAdapter`
- SDK、wire 和浏览器边界具备独立认证

状态：已完成基础版本。

### 2. Durable Operations

- Task Run 使用独立 UUID，不依赖进程内 request ID
- response/progress 映射为持久化终态
- 严格递增、可恢复读取的事件游标
- 真实时间一次性/间隔日程
- 游戏 tick 间隔和游戏时段日程
- 重启、断线、时间跳变和 misfire 行为可预测

状态：已完成持久化 Run、REST 事件游标和 wall/game Scheduler 基础；WebSocket 游标订阅与跨请求取消确认仍需继续。

### 3. Autonomous Director

- 根据人格、关系、世界进度和玩家活动生成中期目标
- 把目标分解为可验证 Task 图
- 同一时间只有一个主任务，生存事件和玩家指令可抢占
- 失败后诊断、恢复或重规划，不盲目重复
- 支持共同目标、主动邀请和适度社交频率预算

### 4. Vanilla 90%

按玩法能力族而非物品数量验收：

- 生存、移动、探索、资源采集
- 制作、熔炼、仓储和装备
- 建造、农业、动物、钓鱼
- 战斗、交易、附魔、铁砧、酿造
- 下界、要塞、末地和 Boss 进程
- 单人长期生存与多人合作

每个公开 Skill 必须有真实完成条件、失败条件和取消语义。占位聊天或只返回 accepted 的动作不计入覆盖率。

### 5. Content Skills

- `core`：底层可信动作
- `general`：原版组合能力
- `content_adapter`：模组配方、机器、菜单和实体语义
- `workflow`：用户声明式任务流程

可执行包保持版本化和签名；数据库只保存 registry 元数据、启用状态和 namespaced Skill state。SQLite 仅用于自动化测试和本地开发，生产环境必须使用 PostgreSQL。

### 6. Operations UI

- 连接与控制权
- Skill 浏览和 schema 表单
- Task Run、进度、终态和取消
- 真实时间/游戏时间日程
- 人格、记忆和玩家关系
- 原版能力矩阵、诊断和事件日志

桌面与移动视图必须可操作，不能依赖直接输入任意 wire 命令完成常规工作流。

### 7. Single-player Acceptance

1. 新建原版生存世界并保持客户端有头运行。
2. 无玩家命令时建立食物、工具、避难所和安全库存。
3. 死亡、断线、缺料、路径失败和夜晚后能够恢复。
4. 稳定推进铁器、钻石、下界、要塞、末地和末影龙里程碑。
5. 连续运行期间所有动作都有 Task Run 和可查询终态。

单人通关是最终综合验收，不替代各能力族的确定性测试。

跨工作流的当前状态、模型上下文治理、记忆管理、后台现代化、生产 PostgreSQL 和身体控制仲裁详见 [LCU Project Tracker](project-tracker.md)。
