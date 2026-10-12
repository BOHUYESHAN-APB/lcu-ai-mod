# LCUMod 完整测试表

> 基线（2026-10-12 本机实测）：后端 `cd backend && python -m unittest discover -s tests` → **1657 tests OK，零 skip**；Java `gradlew.bat check` → **BUILD SUCCESSFUL**，18 个零依赖 `verifyXxx` 自检共 **1226 项**全绿。
> 本表按 **26.3 线的实际代码**写；标为「当前不适用」的小节是上一版从另一条血缘（master 1.21.1）抄来的验收项，本仓库没有对应实现，**不要照它们验收**。

## 启动准备
- [ ] 启动 Minecraft：**Minecraft 26.3 / NeoForge 26.3.0.57-beta**，真实有头客户端
- [ ] 启动 Python 后端：`cd backend && python main.py`
- [ ] 打开浏览器：`http://localhost:8080`

模组是**纯客户端**形态（`LCUMod.commonSetup` 在非 `Dist.CLIENT` 时直接返回，`LCUMod.java:58-61`）。没有 `runtimeRole` 角色矩阵，也没有 `server_fake_player` / `fakePlayerEnabled` —— 同一个 `build/libs/lcumod-0.1.0.jar` 只有一种运行方式。

---

## 1. 基础连接

| # | 测试项 | 预期结果 | 结果 |
|---|--------|----------|------|
| 1.1 | 模组加载 | 日志出现 `[LCUMod] Initializing...`，随后 `[WireServer] Starting on port 25568 (bind on LCU-WireServer thread)`（`WireServer.java:76`）与 `[LCUMod] Ready. Wire server listening on port 25568`（`LCUMod.java:70`）；绑定成功后是 `[WireServer] Listening on 127.0.0.1:25568 (loopback only unless wireBindAddress overrides)`（`WireServer.java:184`） | |
| 1.2 | Python 连接 | 后端日志 `[Backend] Connected to mod at 127.0.0.1:25568`（`server.py:261`）；连不上时是 `Mod not available at 127.0.0.1:25568. Retry in 5s...`（`server.py:275`） | |
| 1.3 | Web 面板加载 | 浏览器显示仪表板，状态数据更新 | |
| 1.4 | 状态推送 | `state_update` 事件按 `stateInterval` 出现 —— 默认 **10 tick（0.5 秒）**（`ModConfig.java:32` → `StateCollector.java:224`） | |

## 2. 移动控制

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 2.1 | 向前走 | `set_control_state {"control":"forward","state":true}` | 玩家向前走，视角不锁死 | |
| 2.2 | 向后走 | `set_control_state {"control":"back","state":true}` | 玩家后退 | |
| 2.3 | 左移 | `set_control_state {"control":"left","state":true}` | 玩家左移 | |
| 2.4 | 右移 | `set_control_state {"control":"right","state":true}` | 玩家右移 | |
| 2.5 | 停止 | `set_control_state {"control":"forward","state":false}` | 玩家停止移动 | |
| 2.6 | 跳跃 | `set_control_state {"control":"jump","state":true}` | 玩家跳跃（`JUMP_COOLDOWN_TICKS=25`，冷却中重发会回 `Jump on cooldown`） | |
| 2.7 | 潜行 | `set_control_state {"control":"sneak","state":true}` | 玩家潜行 | |
| 2.8 | 疾跑 | `set_control_state {"control":"sprint","state":true}` | 玩家疾跑 | |
| 2.9 | 移动到坐标 | `move_to {"x":4,"y":128,"z":27}` | 玩家自动走向目标点 | |
| 2.10 | 视角不受锁死 | 鼠标可自由转动 | 鼠标不每 tick 被覆盖 | |
| 2.11 | 停止所有 | `stop_all` | 所有键释放，移动/挖掘停止 | |

支持的 `control` 值只有 `forward` / `back` / `left` / `right` / `jump` / `sneak` / `sprint`（`ActionExecutor.java:1438-1454`）。

## 3. 挖掘

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 3.1 | 自动工具切换 | `mine_block` | 自动切换到最快工具再开始挖 | |
| 3.2 | 连续挖掘（按住 attack） | 看向方块 → `mine_block` | 按住 attack（`ControlStream`）直到方块消失；消失 → `outcome succeeded`；20s 超时 → 诚实失败 | |
| 3.3 | 停止挖掘 | `stop_digging` | 挖掘立即停止，进度归零 | |
| 3.4 | 挖掘超时 | 对不可破坏方块等到 20s（400 tick） | 松开 attack + `response success=false`（error=`timeout`） | |
| 3.5 | 方块破碎检测 | 挖到碎 → 自动松开 | 松开 attack + 后端收到 `outcome{status:"succeeded"}` | |
| 3.6 | 别名 | `dig_block` | 与 `mine_block` 同一 handler（`ActionExecutor.java:580`） | |

## 4. 容器交互

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 4.1 | 打开容器 | `use_on`（对箱子） | 箱子打开，后端收到 | |
| 4.2 | 读取容器 | `get_container` | 返回容器内物品列表 | |
| 4.3 | 取出物品 | `take_item` | 从容器取到背包 | |
| 4.4 | 放入物品 | `put_item` | 从背包放入容器 | |
| 4.5 | 关闭容器 | `close_container`（别名 `container_close`） | 容器 GUI 关闭 | |
| 4.6 | 槽位移动/点击 | `container_move` / `container_click` | 一律走窗口点击包，不 `setScreen`（覆盖见 `verifyContainer`） | |

## 5. 战斗

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 5.1 | 攻击准星实体 | `attack` | 攻击准星指向的生物 | |
| 5.2 | 攻击指定实体 | `attack_entity {"id":123}` | 解析该实体并攻击 | |
| 5.3 | 战斗反射让路 | 被敌对档生物攻击 | `CombatReflex` 作为每 tick 的最后写者可覆盖当前控制流；`reflex_override` 可抑制窗口 | |

## 6. 背包

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 6.1 | 读取背包 | `get_inventory` | 返回背包所有物品 + 选中槽 | |
| 6.2 | 切换热栏 | `select_hotbar {"index":0}` | 选中热栏第 1 格 | |
| 6.3 | 使用物品（按住） | `use_item`（可选 `{"ticks":4}`） | 按住 use N tick（默认 4）再松开；放方块/开门等 | |
| 6.4 | 自动装备 | `auto_equip`（对准石头） | 切换到镐 | |
| 6.5 | 进食（按住 use，三判据） | `eat`（无必填参数） | 选热栏食物 → 按住 use 直到 饱食度↑ 或 饱和度↑ 或 该槽数量− → 松开；40 tick 超时回 `{ok:false,reason:"timeout"}`；重复调用幂等 | |
| 6.6 | 整理背包 | `sort_inventory` | 已有实现（**不**返回 unsupported） | |

## 7. 实体交互

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 7.1 | 看向实体 | `look_at_entity {"id":123}` | 头转向该实体 | |
| 7.2 | 右键实体 | `use_on_entity {"id":123}` | 右键点击该实体 | |
| 7.3 | 右键交互 | `use_on`（对准拉杆） | 拉杆被拉下 | |
| 7.4 | 放置方块（面校验） | `place_block`（对准地面/方块面；可选 `x/y/z/dir`） | 先看向目标面 → 单次 use(2 tick) → 校验目标位置方块 = 手中方块 → 成功并刷新 state | |

## 8. 聊天

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 8.1 | 发送消息 | `send_chat {"message":"hello"}` | 游戏中发出消息 | |
| 8.2 | 接收消息 | 别人发消息 | `ChatListener` 捕获，不崩溃不断线 | |
| 8.3 | 聊天防刷 | 快速发 2 条 | 第二条被拒绝（`CHAT_COOLDOWN_MS = 1500`，`ActionExecutor.java:715`） | |
| 8.4 | Web 面板聊天 | 别人发消息 | HTML 面板显示聊天消息 | |

## 9. 生存反射（`ModesEngine`，P0–P3）

后台**没有自主开关**：`modes_engine.add_defaults` 只装 P0–P3 四个生存反射（`modes_engine.py:645-656`），原来的自动进食/自动收集/狩猎/跟随主人等自主模式已删除。

| # | 测试项 | 操作 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 9.1 | 启用反射 | `behavior_enable` / 前端开关 | 四个生存反射开始检测 | |
| 9.2 | P0 保命 | 进入岩浆/着火/溺水/虚空 | 立即逃生（P0 每 tick 重申控制流，不走冷却、不被退避挡） | |
| 9.3 | P1 回血 | 低血且背包有治疗物品 | 使用治疗物品 | |
| 9.4 | P2 逃跑 | 低血遇威胁 | 拉开距离（高危险不管血量先撤） | |
| 9.5 | P3 还手 | 被**敌对档**攻击 | 只打敌对档；中立不惹、被动不管、高危险不碰（`modes_engine.py:358-373`） | |
| 9.6 | 禁用反射 | `behavior_disable` | 所有模式停止 | |
| 9.7 | 自提示 | 空闲 + 已设目标 | `agent/self_prompter.py` 的自我提示路径触发 | |

> 上一版 9.2–9.5 写的"自动进食 / 自动收集 / 狩猎动物"等自动触发入口已随 P4–P8 一起删除，本仓库不存在。

## 10. 实体感知

`state_update.entities` 不是逐条带坐标的清单，而是**分级摘要**（`PerceptionView.summarizeEntities`，`PerceptionView.java:135`）：

| # | 测试项 | 检查 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 10.1 | 汇总计数 | `entities.count` | 与附近实体数一致 | |
| 10.2 | 分档计数 | `entities.by_kind` | 按 `MobPolicy.Tier` 分档：`dangerous` / `hostile` / `neutral` / `passive` / `unknown`，外加 `player` / `item` | |
| 10.3 | 最近实体 | `entities.nearest` | 最近一个，**不带坐标** | |
| 10.4 | 威胁列表 | `entities.threats` | 危险/敌对且 ≤16 格，最多 3 条、按距离升序，**只有这些带坐标** | |
| 10.5 | 玩家与掉落物 | `entities.players` / `entities.items` | 玩家**总是带坐标**（≤8）；掉落物聚合 + 最近一件，不带坐标 | |
| 10.6 | 按档过滤扫描 | `scan_entities` | 可用 `kind` 过滤档位 | |

> 认不出的生物 ⇒ `unknown`，**不许默认当成敌对**（`StateCollector.java:538`）。

## 11. 稳定性

| # | 测试项 | 操作 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 11.1 | 聊天断线 | 发送 + 接收各 10 条消息 | 不断线，不崩溃 | |
| 11.2 | 视角锁死 | 移动中动鼠标 | 鼠标可以自由转头 | |
| 11.3 | 后端重启 | 关掉 `python main.py` 重新开 | 自动重连：连不上时每 **5 秒**重试（`server.py:275-276`） | |
| 11.4 | 长时间运行 | 持续连接 30 分钟 | 无内存泄漏，tick 正常 | |

## 12. 持久化 Run / 事件游标 / 日程（**当前不适用**）

上一版这一节的验收前提（`/api/v2/runs`、`/api/v2/events`、`/api/v2/schedules`、`/api/v2/skills`）在本仓库**不存在**：

- 这些路由全部没注册（`backend/tests/test_http_surface.py:122-129` 明确把它们断言在 missing 集合）。
- `backend/agent/task_coordinator.py`、`task_preset_registry.py`、`skill_registry.py`、`decision_scheduler.py` **文件在、不在活路径**（`server.py` / `session.py` / `orchestrator.py` / `planner.py` 都不引用）。
- 后端 V2 面实际只有 `GET /api/v2/control`（恒 `mode="builtin"`）与 `GET /api/v2/audit` 两个**只读**端点。

旧的 12.1–12.6 已删除；详见 [SDK 文档](docs/sdk.md)。

## 13. 记忆管理页（**当前不适用**）

后台**没有**记忆管理页：唯一记忆 HTTP 面是只读 `GET /api/memory`（`server.py:531`，读活路径的 `agent/memory.py`）。`memory_management.py` / `memory_catalog.py` 存在但不在活路径。旧的 13.1–13.8 已删除。

模组侧只把带标签的 POI 如实上报（`state_update.remembered_pois`，含 `category` / `tags`），以及 `state_update.home_bed_bound`（原版当前重生点是否等于记忆中的床）。

---

## 注意事项

1. **控制权语义** —— 默认就是 **AI 持控制权**（进世界即如此）；按 `F12` 切到**玩家控制**；**不会自动切回 AI**（`InputIsolation.autoReturnEnabled = false`，`InputIsolation.java:48`）。
2. **挖掘需要生存模式** —— 创造模式不会破坏方块。
3. **物品收集需要** 在热栏有正确工具。
4. **自动进食** 需要食物在热栏（按住 use ≈1.6s / 32 tick；40 tick 未生效判超时）。
5. **容器交互** 需要玩家面向容器方块。
6. **聊天冷却** 1.5s（`CHAT_COOLDOWN_MS = 1500`，`ActionExecutor.java:715`）防止服务端反刷踢出。
7. **`wireToken` 现在是接线了的**（2026-10-12 修）—— `LCUMod.commonSetup` 会在 `WIRE.start()` **之前**把 `ModConfig.WIRE_TOKEN` 交给 `WireServer.setRequiredToken`，并打日志：武装了报位数（不打印 token 本身），**空值打 WARN** 明说端口不鉴权。所以：
   - 空值（默认）= 老行为，不鉴权（回环/单机安全，端口仅 `127.0.0.1`）；
   - 要鉴权**两边都得设成同一个值**：模组 `config/lcumod-local.toml` 的 `wireToken` + 后端环境变量 `LCU_WIRE_TOKEN`（或 `.local/wire_token` 文件）；不一致 ⇒ 首帧 `hello` 不通过、连接被关掉（后端日志会看到重连）。
   - 后端那半本来就是好的：`protocol/wire_client.py:293-306` 读 token 并作为首帧 `hello` 发出（`:353`）。
   - 回归：`verifyWalk` 的 `wireTokenArmedChecks`（启动真读了配置、武装发生在 `start()` 之前、武装与空值都有日志）。
8. **按住类动作自检** — `gradlew verifyHold` → **43 checks passed**（hold N tick 到期松开 / 条件命中即松开 / 超时带原因 / AI 关闭·玩家接管·反射覆盖立即释放 / eat 幂等 / `reflex_eat`）；已接入 `check`，随 `build` 跑。
9. **安全/防护自检** — `gradlew verifyGuard` → **109 checks passed**（出站文本清洗 / 指令硬黑名单 / 区域保护球·长方体命中与"保护优先、多区取最保守、无保护项不拦" / `click_screen_button` 的 `no_screen`·`no_match`·让路 / `place_block` 物品不在背包即失败不替换 / 导航不搭方块的静态扫描）；已接入 `check`。
10. **线协议预留自检** — `gradlew verifyProto` → **49 checks passed**（`{v,type,seq,ts,payload}` 封装 / 未知字段与未知 `type` 忽略 / 坏 JSON 返回 null / `Body` 抽象 0 处 `net.minecraft` / `PROTOCOL_VERSION` + 6 个能力位 / token 鉴权若干项 / `WireServer` 委托 `WireAuth` / 有闸门且闸门在命令分发**之前** / 未鉴权连接有读超时 / 配置键 `wireToken` 默认空）；已接入 `check`。
11. **聊天分类自检** — `gradlew verifyChat` → **59 checks passed**；已接入 `check`。
12. **容器 UI 自检** — `gradlew verifyContainer` → **98 checks passed**；已接入 `check`。
13. **站点与路径自检** — `gradlew verifyRoute` → **132 checks passed**；已接入 `check`。
14. **配方检索自检** — `gradlew verifyRecipe` → **106 checks passed**；已接入 `check`。
15. **落地水自检** — `gradlew verifyMlg` → **91 checks passed**；已接入 `check`。
16. **跨语言线协议集成测试（真 Java ⇄ 真 Python，仓库里唯一一条）** — `cd backend && python -m unittest discover -s tests/integration -t tests/integration -v` → **7 tests OK**。它自己起 `gradlew runWireFixture`：headless 夹具 `WireServerFixtureMain`（在 **test sourceset**，`unzip -l build/libs/lcumod-0.1.0.jar` 里没有它）跑的是**生产** `WireServer`；Python 侧走**真** `protocol/wire_client.py`。端口随机（`-PwireFixturePort=0`）。夹具起不来 ⇒ 整个测试类 error（真失败，绝不是 skip）。
    - **没覆盖**：游戏侧消费方 —— `ActionExecutor` 的命令执行、`StateCollector` 的真实状态内容、tick 驱动的输入/动作。
    - 它**不进** `check`/`build`（长驻服务）；默认 `discover -s tests` 也**不会**递归进 `tests/integration`（该目录**故意没有 `__init__.py`**），所以单测基线是 **1657 零 skip**（2026-10-12 实测）。CI 有独立 `integration` job 跑上一条命令。
17. **行走/视角自检 + 感知分级/按需扫描 + wireToken 武装** — `gradlew verifyWalk` → **346 checks passed**（导航中后端 `look` **不覆盖**路径朝向 / 无导航时后端 `look` 作为**绝对值**生效 / 每 tick 最多 10°·6° **逐步转**、18 tick 内收敛 / 转不过去 ⇒ **如实停步上报** / 重算退避且相同结果只报一次 / **静态扫描**：`Pathfinder` 里不许出现 `setYRot`·`setXRot`，`InputIsolation` 是唯一视角写者 / 感知摘要 + `scan_entities` 的档位边界与上限 / **启动时真的把 `wireToken` 武装上**且空值要 WARN）；已接入 `check`。
18. **村民交易自检** — `gradlew verifyTrade` → **27 checks passed**；已接入 `check`。
19. **真建造自检** — `gradlew verifyBuild` → **22 checks passed**；已接入 `check`。

> 零依赖自检共 **18 套 1226 项**（`verifyLogic 21`、`verifyTitle 11`、`verifyChat 59`、`verifyFinder 18`、`verifyPhrase 16`、`verifyGate 12`、`verifyHold 43`、`verifyContainer 98`、`verifyRoute 132`、`verifyRecipe 106`、`verifyGuard 109`、`verifyProto 49`、`verifyMlg 91`、`verifyWalk 346`、`verifyTrade 27`、`verifyBuild 22`、`verifyMixin 53`、`verifyLang 13`）。跑单个任务**必须加 `--rerun` 且逐个跑**（一次跑多个会被判 UP-TO-DATE 静默跳过）。
