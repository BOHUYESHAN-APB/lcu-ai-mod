# LCU 部署与运行角色

## 结论

LCU 支持仅客户端安装。使用真实有头 AI 身体时，只在 AI 登录所使用的 NeoForge 客户端安装 LCU，目标 Minecraft 服务器、其他普通玩家客户端均不要求安装。

服务器安装 LCU 只用于未来的 `server_fake_player` 形态。该形态当前尚未实现，不能代替真实客户端身体。

## 部署矩阵

| 角色 | `runtimeRole` | LCU 安装位置 | 服务器是否安装 | 能力 |
|------|---------------|--------------|----------------|------|
| AI 实体客户端 | `body_client` | AI 的真实 Minecraft 客户端 | 不需要 | 状态感知、动作执行、F12 控制、WireServer |
| 普通玩家对话客户端 | `player_client` | 需要游戏内对话功能的玩家客户端 | 不需要 | `P` 键受限对话；无执行器和 operator token |
| 服务端假玩家 | `server_fake_player` | Minecraft 服务端 | 需要 | 当前仅安全占位，执行器未实现 |

同一台目标服务器可以完全不安装 LCU。AI 客户端仍以一个真实账号正常登录，服务器只会看到该账号产生的标准移动、交互、聊天和菜单操作。

## 为什么服务器不需要安装

当前客户端身体没有注册需要服务端同步的 LCU 物品、方块、实体、菜单或自定义网络协议。Python 后端连接的是 AI 客户端本机提供的 JSONL WireServer，而不是 Minecraft 服务端。

`neoforge.mods.toml` 中 NeoForge 和 Minecraft 依赖的 `side="BOTH"` 表示：LCU 在某个物理端被加载时，该端必须具备对应依赖。它不表示远端服务器也必须安装 LCU。

目标服务器或整合包仍可能自行限制允许加入的客户端模组，这不属于 LCU 的协议要求。

## AI 实体客户端配置

把 JAR 放入 AI 实例的 `mods` 目录。首次加载生成配置后，编辑 `config/lcumod-common.toml`：

```toml
runtimeRole = "body_client"
wirePort = 25568
wireToken = "使用独立随机令牌"

# Public-server-oriented defaults; enable categories only when server rules permit them.
allowMovementAutomation = true
allowWorldAutomation = false
allowInventoryAutomation = false
allowAutomatedCombat = false
allowChatAutomation = true
enableAutonomousBehaviors = false
enableActivitySignals = false
reportProgrammaticActivity = false
runInBackground = false
autoRespawn = false
collectSurroundings = false
```

Python 后端的 `MOD_WIRE_TOKEN` 必须与 `wireToken` 相同。WireServer 应只监听和服务于受信环境，不要把空令牌端口暴露到局域网或公网。

这些选项按能力类别控制，不是单一“安全模式”。私服或二次开发可逐项开启，但不得把
配置为 `true` 解释成目标服务器授权。已有配置文件会保留旧值，升级后必须人工复核。
反作弊与服务器规则边界见 [Server Policy And Anti-Cheat Safety](server-policy-safety.md)。

## 模型密钥存储

后台管理台填写的模型 `base_url` 和 `api_key` 持久化在：

```text
backend/.local/config.json
```

服务端 API 默认会把密钥脱敏成 `***`，但该文件在本机磁盘上是明文 JSON。
`backend/.local/` 已加入 `.gitignore`，不会被普通 `git add` 纳入提交；旧配置
路径 `backend/config.json`、`backend/.env` 和 `backend/.api_key` 也已忽略。
不得使用 `git add -f` 强制提交这些文件。正式部署应改用权限受限的 secrets
manager 或操作系统凭据存储，并定期轮换密钥。

默认角色为 `player_client`。该默认值用于防止普通玩家客户端意外启动动作执行器；AI 实体实例必须显式改为 `body_client`。

## 纯客户端模式边界

- AI 只能感知客户端实际收到的数据，不能读取未加载区块或服务端私有状态。
- 所有动作仍受游戏模式、服务端权限、反作弊、冷却、距离和菜单校验约束；这不代表
  自动化符合目标服务器规则，也不保证不会被拒绝或踢出。
- 真实客户端断开或关闭后，AI 身体不再存在于服务器中。
- 纯客户端模式不提供服务端假玩家、离线常驻、管理员权限或服务器验证的玩家身份网关。
- 服务端不安装 LCU 时，不能使用未来的 `server_fake_player` 能力，但不影响 `body_client`。

## 当前验证状态

- Java 单元测试和真实 Java-to-Python Wire 集成测试已通过。
- 安装 LCU 的 NeoForge 专用服务端可正常启动，证明公共入口不会错误加载客户端类。
- 模组代码扫描确认没有必须由目标服务器配套注册的 LCU 游戏网络通道或注册表内容。
- “AI 客户端安装 LCU、目标服务器不安装 LCU”的真实多人登录和动作测试仍需在目标测试服务器完成。
