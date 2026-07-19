# LCU 模组兼容矩阵

本文记录已识别模组、兼容边界、实现状态和待验证问题。只有完成自动化测试与真实整合包测试的项目才能标记为“已验证”。

## 当前测试实例

测试实例：FTB StoneBlock 4，Minecraft 1.21.1，NeoForge 21.1.233。

| 模组 | 检测版本 | 兼容状态 | 当前结论 |
|------|----------|----------|----------|
| WATUT | `1.21.0-1.2.7` | 可选，待整合包验证 | 默认不报告程序活动；仅在服务器规则允许且显式开启时调用 WATUT 活动接口 |
| Simple Tomb | `1.21.1-1.4.4` | 调研中 | 已确认墓碑、`grave_key`、死亡记录与传送相关实现，尚未接入恢复流程 |
| Inventory Sorter | `1.21.1-24.0.24` | 调研中 | 已识别当前 R/鼠标中键整理模组，尚未确认稳定 API |
| Inventory Essentials | `1.21.1-21.1.16` | 未开始 | 可能影响鼠标库存操作，需要与 Inventory Sorter 一并验证 |
| Player Animation Library | `2.0.4` | 无需直接适配 | 动画依赖库，不负责 idle 判定 |
| Curios | `9.5.1+1.21.1` | 已实现，待整合包验证 | 可选反射读取饰品槽，不形成编译或服务端硬依赖 |

## WATUT

WATUT 的 NeoForge 输入链是 `InputEvent.Key` / `InputEvent.MouseButton.Post` 到
`PlayerStatusManagerClient.onKey()` / `onMouse()`，最终调用公开但非稳定 API
`onAction()` 清除内部 idle tick。LCU 直接写入 Minecraft 按键状态不会触发这条链。

当前使用可选反射适配：仅在 `watut` 已加载、LCU 动作确实执行且
`reportProgrammaticActivity=true` 时调用
`WatutMod.getPlayerStatusManagerClient().onAction()`。不得伪造全局 GLFW 或 NeoForge
输入事件，避免误导其他模组。

已知边界：这只能清除 WATUT 的低头、头顶 idle 粒子和 Tab 状态，不能重置
Minecraft 服务端或服务器插件自己的 AFK 计时。LCU 不得绕过服务端 AFK 策略；
`enableActivitySignals` 与 `reportProgrammaticActivity` 默认关闭，只能在规则明确允许时开启。

源码：

- <https://github.com/Corosauce/WATUT/blob/1.21.0/src/main/java/com/corosus/watut/loader/neoforge/ClientEvents.java>
- <https://github.com/Corosauce/WATUT/blob/1.21.0/src/main/java/com/corosus/watut/PlayerStatusManagerClient.java>

## Simple Tomb

当前实例包含 `simpletomb:grave_key`、墓碑方块、玩家墓碑记录和传送实现。
恢复流程必须按状态机执行：

1. 记录死亡维度、坐标、死亡序列和当时背包基线。
2. 复活后识别有效墓碑钥匙及其绑定记录。
3. 若配置与权限允许钥匙跨维度传送，优先使用钥匙。
4. 若不允许跨维度，先通过已确认的传送门进入目标维度，再使用钥匙或寻路。
5. 到达墓碑后验证墓碑归属，再交互取回装备。
6. 对比恢复前后库存，重新评估装备、快捷栏和背包布局。

默认禁用自动 TP，必须由后台显式开启。传送失败、维度不匹配、钥匙未绑定、
墓碑归属不明或权限拒绝时必须停止并报告，不得循环使用钥匙或滥发命令。

已从当前 `1.4.4` JAR 字节码和实例配置确认：

- `simpletomb:grave_key` 保存 `GlobalPos`，包含完整维度 ID 和方块坐标。
- 长按使用时总时长为 86 tick，在剩余 1 tick 时传送，约需 4.25 秒。
- 传送前硬性要求钥匙维度等于玩家当前维度，不能直接跨维度。
- 当前生存传送距离配置为 128；超过 128 格不会传送。`-1` 才表示同维度不限距离。
- `openOnUse=true` 表示手持钥匙点击墓碑时调用墓碑激活逻辑，不是隔空打开。

待确认墓碑激活后的精确装备恢复/冲突槽规则和稳定公开 API。完成真实服务器验证前不标记兼容。

## Inventory Sorter

当前版本通过 GUI 键鼠事件选中槽位，再发送 `inventorysorter:action_message` 给服务端；
排序在服务端 `SortingHandler` 执行。它没有面向其他模组的公开排序 API，IMC 只用于
槽位/容器黑名单。LCU 不能仅调用客户端排序类，也不应伪造 R/鼠标中键。

兼容方案需要二选一：版本化构造其 action payload，且确认服务端也安装同协议版本；
或由 LCU 使用可验证的标准容器点击事务实现自己的确定性布局。当前 `sort_inventory`
仍不得宣称完成，适配完成前应返回明确的 unsupported 错误。

## 后续墓碑实现

其他墓碑或尸体模组使用独立 adapter，不根据方块显示名猜测。每个 adapter 至少声明：

- mod id 与版本范围；
- 死亡记录来源；
- 维度与坐标语义；
- 传送权限和跨维度能力；
- 归属验证；
- 取回终态与库存差量；
- 可恢复失败和永久失败条件。
