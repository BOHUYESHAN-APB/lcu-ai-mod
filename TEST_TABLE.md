# LCUMod 完整测试表

## 启动准备
- [ ] 启动 Python 后端: `cd backend && python main.py`
- [ ] 启动 Minecraft (FTB StoneBlock 4)
- [ ] 打开浏览器: `http://localhost:8080`

同一个 `build/libs/lcumod-0.1.0.jar` 必须按以下角色矩阵分别验收：

| 物理侧 | `runtimeRole` | 预期结果 | 结果 |
|--------|---------------|----------|------|
| 客户端 | `body_client`（新配置默认） | WireServer 和真实客户端身体启动，`P` 对话窗口不启用 | |
| 客户端 | `player_client` | `P` 对话窗口启用，WireServer/F12/动作执行器不启动 | |
| 专用服务端 | `server_fake_player` + `fakePlayerEnabled=false` | 服务端正常启动，不声明假玩家可用 | |
| 专用服务端 | `server_fake_player` + `fakePlayerEnabled=true` | 当前构建明确拒绝启动，直到服务端执行器实现 | |

---

## 1. 基础连接

| # | 测试项 | 预期结果 | 结果 |
|---|--------|----------|------|
| 1.1 | 模组加载 | 日志出现 `[LCUMod] Initializing...` + `[WireServer] Started on port 25568` | |
| 1.2 | Python 连接 | 后端日志 `[Backend] Connected to mod at 127.0.0.1:25568` | |
| 1.3 | Web 面板加载 | 浏览器显示仪表板，状态数据更新 | |
| 1.4 | 状态推送 | `state_update` 事件每 ~5s 出现（`← event: state_update`） | |

## 2. 移动控制 (Mineflayer physics.js)

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 2.1 | 向前走 | `set_control_state {"control":"forward","state":true}` | 玩家向前走，视角不锁死 | |
| 2.2 | 向后走 | `set_control_state {"control":"back","state":true}` | 玩家后退 | |
| 2.3 | 左移 | `set_control_state {"control":"left","state":true}` | 玩家左移 | |
| 2.4 | 右移 | `set_control_state {"control":"right","state":true}` | 玩家右移 | |
| 2.5 | 停止 | `set_control_state {"control":"forward","state":false}` | 玩家停止移动 | |
| 2.6 | 跳跃 | `set_control_state {"control":"jump","state":true}` | 玩家跳跃 | |
| 2.7 | 潜行 | `set_control_state {"control":"sneak","state":true}` | 玩家潜行 | |
| 2.8 | 疾跑 | `set_control_state {"control":"sprint","state":true}` | 玩家疾跑 | |
| 2.9 | 移动到坐标 | `move_to {"x":4,"y":128,"z":27}` | 玩家自动走向目标点 | |
| 2.10 | 视角不受锁死 | 鼠标可自由转动 | 鼠标不每 tick 被覆盖 | |
| 2.11 | 停止所有 | `stop_all` | 所有键释放，移动/挖掘停止 | |

## 3. 挖掘 (Mineflayer digging.js)

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 3.1 | 自动工具切换 | `mine_block` | 自动切换到最快工具再开始挖 | |
| 3.2 | 连续挖掘（按住 attack） | 看向方块→`mine_block` | 按住 attack（`ControlStream.holdUntil`）直到方块消失；消失→`outcome succeeded`；20s 超时→诚实失败 | |
| 3.3 | 停止挖掘 | `stop_digging` | 挖掘立即停止，进度归零 | |
| 3.4 | 挖掘超时 | 对不可破坏方块等到 20s（400 tick） | 松开 attack + `response success=false`（error=`timeout`），任务 failed | |
| 3.5 | 方块破碎检测 | 挖到碎 → 自动松开 | 松开 attack + 后端收到 `outcome{status:"succeeded"}` | |

## 4. 容器交互 (Mineflayer chest.js)

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 4.1 | 打开容器 | `use_on` (对箱子) | 箱子打开，后端收到 | |
| 4.2 | 读取容器 | `get_container` | 返回容器内物品列表 | |
| 4.3 | 取出物品 | `take_item {"slot":0}` | 从容器第1格取出到背包 | |
| 4.4 | 放入物品 | `put_item {"slot":0}` | 从背包放入容器第1格 | |
| 4.5 | 关闭容器 | `close_container` | 容器 GUI 关闭 | |

## 5. 战斗 (Mindcraft modes.js)

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 5.1 | 攻击准星实体 | `attack` | 攻击准星指向的生物 | |
| 5.2 | 攻击指定实体 | `mine_block` 附近有怪物 | 自动防御触发 | |

## 6. 背包

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 6.1 | 读取背包 | `get_inventory` | 返回背包所有物品+选中槽 | |
| 6.2 | 切换热栏 | `select_hotbar {"index":0}` | 选中热栏第1格 | |
| 6.3 | 使用物品（按住） | `use_item`（可选 `{"ticks":4}`） | 按住 use N tick（默认 4）再松开；放方块/开门等 | |
| 6.4 | 自动装备 | `auto_equip` (对准石头) | 切换到镐 | |
| 6.5 | 进食（按住 use，三判据） | `eat`（无必填参数） | 选热栏食物→按住 use 直到 饱食度↑ 或 饱和度↑ 或 该槽数量−→松开；40 tick 超时回 `{ok:false,reason:"timeout"}`；重复调用幂等（不重复切槽/按键） | |

## 7. 实体交互

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 7.1 | 看向实体 | `look_at_entity {"id":123}` | 头转向该实体 | |
| 7.2 | 右键实体 | `use_on_entity {"id":123}` | 右键点击该实体 | |
| 7.3 | 右键交互 | `use_on` (对准拉杆) | 拉杆被拉下 | |
| 7.4 | 放置方块（面校验） | `place_block`（对准地面/方块面；可选 `x/y/z/dir`） | 先看向目标面→单次 use(2 tick)→校验目标位置方块=手中方块→成功并刷新 state | |

## 8. 聊天

| # | 测试项 | 命令 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 8.1 | 发送消息 | `send_chat {"message":"hello"}` | 游戏中发出消息 | |
| 8.2 | 接收消息 | 别人发消息 | ChatListener 捕获，不崩溃不断线 | |
| 8.3 | 聊天防刷 | 快速发2条 | 第二条被拒绝（冷却1.5s） | |
| 8.4 | Web 面板聊天 | 别人发消息 | HTML 面板显示聊天消息 | |

## 9. 自主行为 (ModesEngine)

| # | 测试项 | 操作 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 9.1 | 启用自主模式 | 前端点"行为启用" 或 `behavior_enable` | 8种模式开始检测 | |
| 9.2 | 自动进食 | 饱食度<14 + 背包有食物 | 自动吃食物 | |
| 9.3 | 自动防御 | 附近有敌对生物<8格 | 转头+攻击 | |
| 9.4 | 自动收集 | 物品掉落<4格 | 走过去捡 | |
| 9.5 | 狩猎动物 | 饱食度<12 + 附近有动物 | 转头+攻击动物 | |
| 9.6 | 禁用模式 | `behavior_disable` | 所有模式停止 | |
| 9.7 | SelfPrompter | 空闲15秒+已设目标 | LLM 自动触发自我提示 | |

## 10. 实体检测

| # | 测试项 | 检查 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 10.1 | 敌对生物检测 | `get_state` 的 entities | 附近僵尸/骷髅标记为 `type:"hostile"` | |
| 10.2 | 动物检测 | `get_state` 的 entities | 附近牛/羊标记为 `type:"animal"` | |
| 10.3 | 掉落物检测 | `get_state` 的 entities | 附近物品标记为 `type:"item"` | |
| 10.4 | 玩家检测 | `get_state` 的 entities | 其他玩家标记为 `type:"player"` | |

## 11. 稳定性

| # | 测试项 | 操作 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 11.1 | 聊天断线 | 发送+接收各10条消息 | 不断线，不崩溃 | |
| 11.2 | 视角锁死 | 移动中动鼠标 | 鼠标可以自由转头 | |
| 11.3 | 后端重启 | 关掉 python main.py 重新开 | 自动重连 (5s) | |
| 11.4 | 长时间运行 | 持续连接30分钟 | 无内存泄漏，Tick 正常 | |

## 12. Durable Operations

| # | 测试项 | 操作 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 12.1 | 持久化 Run | 通过 V2 执行 `core.jump` | 返回 UUID run，response 后为 `succeeded` | |
| 12.2 | Progress Run | 执行 `general.craft_item` | 接受响应后为 `running`，终态 progress 后完成 | |
| 12.3 | 断线终态 | 运行中断开身体 | run 变为 `unknown`，重连后不自动重放 | |
| 12.4 | 真实时间日程 | 创建 60 秒 interval | 每个到期周期最多创建一个 run，重启后仍存在 | |
| 12.5 | 游戏时段日程 | 创建 `time_of_day_tick=1000` | 每个游戏日只触发一次 | |
| 12.6 | 事件游标 | 使用 `after` 重新读取 | cursor 严格递增且无重复 | |

## 13. 记忆管理（SQLite 开发闭环）

| # | 测试项 | 操作 | 预期结果 | 结果 |
|---|--------|------|----------|------|
| 13.1 | 浏览与筛选 | 后台“记忆”页切换类别、状态并搜索 | 分页记录、详情、来源哈希与作用域正确 | 自动测试通过 |
| 13.2 | 脱敏导出 | 点击“导出” | 下载 JSONL，凭据字段显示为 `***` | 自动测试通过 |
| 13.3 | 持久摘要 | 点击“摘要”，确认 preview 后保存 | 新增 summary，来源记录数量不变 | 自动测试通过 |
| 13.4 | 归档 | 活动记录详情点击“归档”，输入确认文本 | 记录只出现在“已归档”，审计增加一条 | 自动测试通过 |
| 13.5 | 软删除与恢复 | 已归档记录软删除，再从“已删除”恢复 | 原始数据不丢失，最终回到“活动” | 自动测试通过 |
| 13.6 | 保留规则 | 保存归档/软删除天数，点击“预览并执行” | 只处理满足年龄且超过 `min_keep` 的记录 | 自动测试通过 |
| 13.7 | 陈旧预览保护 | preview 后改变记忆，再提交旧 preview | 返回冲突，不执行旧操作 | 自动测试通过 |
| 13.8 | 简单 UI 验收 | 在本地后台完成 13.1、13.4、13.5 | 状态计数与审计实时更新，页面无报错 | 待人工测试 |

---

## 注意事项

1. **控制权语义（已按代码实测更正）** — 默认就是 **AI 持控制权**（进世界即如此，鼠标**不会被抓走**、你可以自由用桌面 ✓）；按 `F12` 切到**玩家控制**（此时才会抓鼠标让你转视角 ✓）；**不会自动切回 AI** ✗（`InputIsolation.autoReturnEnabled=false` ✓）。这条由 `InputIsolation` 的类说明与 `toggleControl()` 实现实证 ✓；旧版本这里写成"按 F12 才把控制权交给 AI"是**反的** ✗
2. **挖掘需要生存模式** — 创造模式不会破坏方块
3. **物品收集需要** 在热栏有正确工具
4. **自动进食** 需要食物在热栏（按住 use ≈1.6s / 32 tick；40 tick 未生效判超时）
5. **容器交互** 需要玩家面向容器方块
6. **聊天冷却** 1.5s 防止服务端反刷踢出
7. **记忆删除为软删除** — SQLite 开发阶段不会硬删除消息库或 `memory.json` 原始记录，可从“已删除”恢复
8. **按住类动作自检** — `gradlew verifyHold` → **43 checks passed**（hold N tick 到期松开 / 条件命中即松开 / 超时带原因 / AI关闭·玩家接管·反射覆盖立即释放 / eat 幂等 / `reflex_eat`）；已接入 `check`，随 `build` 跑
9. **安全/防护自检** — `gradlew verifyGuard` → **109 checks passed**（出站文本清洗 `//`·全角空格·行首空白 / 指令硬黑名单 `kill kick ban …` 的大小写·命名空间·前导斜杠·多余空白 / 区域保护球·长方体命中与"保护优先、多区取最保守、无保护项不拦"·四个破坏/放置入口都装保护 / `click_screen_button` 的 `no_screen`·`no_match`·让路 / `place_block` 物品不在背包即失败不替换 / 导航不搭方块的静态扫描）；已接入 `check`，随 `build` 跑
10. **线协议预留自检** — `gradlew verifyProto` → **49 checks passed**（`{v,type,seq,ts,payload}` 封装 / 未知字段与未知 `type` 忽略 / 坏 JSON 返回 null / `Body` 抽象 0 处 `net.minecraft` / `PROTOCOL_VERSION` + 6 个能力位 / **token 鉴权 12 项**：空 token 放行·匹配·空白容错·坏 token 拒·空与 null 拒 / `WireServer` 委托 `WireAuth` / `WireAuth` 无 gson·无 MC·无日志 / 有闸门且闸门在命令分发**之前** / 未鉴权连接有读超时 / 配置键 `wireToken` 默认空）；已接入 `check`
11. **聊天分类自检** — `gradlew verifyChat` → **59 checks passed**（按翻译键分类 public_text/whisper_in/whisper_out/announcement/system/emote/unknown；未知键→公屏；`whisper` 只给 whisper_in；`whisper_out` 不带收件人名）；已接入 `check`
12. **容器 UI 自检** — `gradlew verifyContainer` → **98 checks passed**（AI 自己开容器时只取消自己的界面 / 玩家自己开着界面时让路 / 容器点击一律走窗口点击包、不 `setScreen`）；已接入 `check`
13. **站点与路径自检** — `gradlew verifyRoute` → **132 checks passed**（站点滞回 + keepalive **绝不写移动键** / 路径录制 <2 或 >2000 点拒绝 / 逐点接力 ≤9 格 / 失败如实上报且不搭桥自救）；已接入 `check`
14. **配方检索自检** — `gradlew verifyRecipe` → **106 checks passed**（id 精确 → 中英文名 → tag；歧义回候选不猜；工作站全覆盖；酿造如实 `unsupported_station`）；已接入 `check`
15. **落地水自检** — `gradlew verifyMlg` → **91 checks passed**（伤害 = 原版 `floor((落距 − SAFE_FALL_DISTANCE) × 倍率)` / 羽落 3 EPF、保护 1 EPF/件、总上限 20、抗性先算 / 读不到减免时按**裸伤害**判的保守分支 / 13 类安全缓冲直接让路 / 不致命绝不放水）；已接入 `check`
16. **跨语言线协议集成测试（真 Java ⇄ 真 Python，仓库里唯一一条）** — `cd backend && python -m unittest discover -s tests/integration -t tests/integration -v` → **7 tests OK**（基类 5 条 + **token 闸门 2 条** ✓）。它自己起 `gradlew runWireFixture`：headless 夹具 `WireServerFixtureMain`（在 **test sourceset**，`unzip -l build/libs/lcumod-0.1.0.jar` 里没有它 ✓）跑的是**生产** `WireServer`——真 accept 循环、真 UTF-8 JSONL 读写、真共享 `commandQueue`、真 `sendEvent`/`sendResponse`/`sendProgress`/`sendOutcome` 与 reader 的 `ControlStream` 分支；Python 侧走**真** `protocol/wire_client.py` ✓。端口随机（`-PwireFixturePort=0`，夹具自选空闲端口后打印 `READY <port>`）✓。夹具起不来 ⇒ **整个测试类 error（真失败，绝不是 skip）** ✓。
    - **没覆盖** ✗：游戏侧消费方——`ActionExecutor` 的命令执行、`StateCollector` 的真实状态内容、`tick` 驱动的输入/动作。夹具只能替 `commandQueue` 的消费者答话（消息形状照 `ActionExecutor` 抄），**替不了游戏** ✗。
    - 它**不进** `check`/`build`（长驻服务）；默认 `discover -s tests` 也**不会**递归进 `tests/integration`（该目录**故意没有 `__init__.py`**，所以单测基线是 **1452 零 skip**（2026-10-12 实测 ✓；当天从 1209 起，经真建造 / 三件小工具 / 复合工具 / HTTP 面 / web 补全 / 跨轮复合体 / 过来-vs-跟着 / 周期日志限流 / 家与床的记忆标签各批增量到此）✓）；CI 有独立 `integration` job 跑上一条命令 ✓。**2026-10-11 实测：GitHub Actions 上 6/6 job 全绿**（`java` / `python` / `integration` × ubuntu+windows，运行 `38147958906` ✓）—— 之前那只是推断，现在有实测了 ✓。
    - **token 闸门 2 条**（2026-10-11 加 ✓）：夹具带 `-PwireFixtureToken` 起 ✓ ⇒ ① 后端首帧发对 token ⇒ 夹具打印 `Hello accepted` 且 `state_update` 正常到 ✓；② 发**坏 token** ⇒ 夹具打印 `Rejected … bad token` ✓ 且被拒后**一个事件都不到** ✓（反证"假拒绝"✗）。
17. **行走/视角自检** — `gradlew verifyWalk` → **123 checks passed**（下表是它一开始的核心项，之后逐批加了 come-vs-follow / 出手安全 / 记忆标签；(导航中后端 `look` **不覆盖**路径朝向 / 无导航时后端 `look` 作为**绝对值**生效 / 每 tick 最多 10°·6° **逐步转**、18 tick 内收敛、跨 ±180° 缝走短路 / 转不过去 ⇒ **如实停步上报**（不原地无限转 ✗）/ 重算退避 2s 且相同结果只报一次 / **静态扫描**：`Pathfinder` 里**不许出现** `setYRot`·`setXRot`，`InputIsolation` 是唯一视角写者）；已接入 `check`
18. **村民交易自检** — `gradlew verifyTrade` → **27 checks passed**（挑报价：按序号 ✓ / 按结果物**大小写无关**模糊匹配 ✓ / 不给提示⇒第一条**还有货**的 ✓；挑不中**一律给机器可读 code**：`no_offers`·`offer_index_out_of_range`·`offer_not_found`·`all_sold_out` ✓ 绝不编一条 ✗ / 成交判定 = 结果槽**真被拿走** ✓（`tradeHappened`）否则如实 `trade_no_effect` ✗ / 槽位常量 0·1·2 与 `MerchantMenu` 源码实证一致 ✓ / **静态扫描**：`TradePlan` 零依赖（看 import ✓）、走原版选报价三步（`setSelectionHint`+`tryMoveItems`+`ServerboundSelectTradePacket` ✓）、**全程不开界面** ✓、六个失败 code 真在源码里 ✓、结果槽点击走窗口点击包 ✓）；已接入 `check`
19. **真建造自检** — `gradlew verifyBuild` → **22 checks passed**（4 种结构算块数：`wall` 3×2=6 ✓、`pen` 3×1 = 3×3 空心围栏**留一格门**=7 ✓、`platform` ✓、`pillar` ✓ / **自下而上**排序（层号不许回退 ✓）/ 认不出的结构名与空名**如实拒**（`unknown_structure` ✓）/ 超单次上限**如实拒**（81>64 ⇒ `too_many_blocks` ✓）/ **够得着判定**：站在基准点 4 块全够 ✓、离 40 格一块都不够 ✓ 且 `beyondReach` 正好是补集 ✓ / **静态扫描**：`BuildPlan` 零依赖 ✓、**`place_block` 与 `build` 共用同一条放置路径**（都走 `beginPlacement` ✓ 不许各写一套 ✗）、全程不开界面 ✓、五个失败 code 真在源码里 ✓、逐块进度与失败汇总都在 ✓）；已接入 `check`

> 零依赖自检共 **18 套 975 项**（`verifyLogic 21`、`verifyTitle 11`、`verifyChat 59`、`verifyFinder 18`、`verifyPhrase 16`、`verifyGate 12`、`verifyHold 43`、`verifyContainer 98`、`verifyRoute 132`、`verifyRecipe 106`、`verifyGuard 109`、`verifyProto 49`、`verifyMlg 91`、`verifyWalk 123`、`verifyTrade 27`、`verifyBuild 22`、`verifyMixin 25`、`verifyLang 13`）。跑单个任务**必须加 `--rerun` 且逐个跑**（一次跑多个会被判 UP-TO-DATE 静默跳过 ✗）。
