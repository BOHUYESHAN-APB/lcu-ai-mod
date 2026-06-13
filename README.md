# LCU Mod — AI-Powered Minecraft Companion

一个基于 NeoForge 的 Minecraft 模组，配合 Python 后端实现 AI 驱动的自主行为系统。

## 项目结构

```
lcumod/
├── src/                    # Minecraft 模组 (NeoForge, Java)
│   └── main/java/com/lcu/lcumod/
│       ├── action/         # 动作执行器、寻路、POI 记忆
│       ├── state/          # 状态收集器
│       └── network/        # 网络通信 (WireServer)
├── backend/                # AI 后端 (Python, FastAPI)
│   ├── agent/              # 会话管理、规划器、模式引擎
│   ├── protocol/           # 通信协议
│   └── web/                # Web UI
└── docs/                   # 文档
```

## 核心能力

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

## 协议

本项目采用**双协议**结构：

| 部分 | 协议 | 说明 |
|------|------|------|
| Minecraft 模组 (`src/`) | **MIT** | 前端模组代码，自由使用 |
| Python 后端 (`backend/`) | **AGPL-3.0** | 后端服务代码，修改后需开源 |

详见：
- [MIT License](LICENSE) — 模组部分
- [AGPL License](backend/LICENSE) — 后端部分

## 快速开始

### 前置要求
- Java 21+
- Python 3.11+
- Minecraft 1.20.2 (NeoForge)

### 构建模组
```bash
.\gradlew.bat build
```

### 启动后端
```bash
cd backend
pip install -r requirements.txt
python main.py
```

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
python -m unittest discover -s tests
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
