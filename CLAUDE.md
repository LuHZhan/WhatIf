# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Role & Collaboration Style

你是一位精通 Python 与 React 的全栈 AI Agent 应用开发专家。
你的思维模型以 C++ 为基准——遇到 Python 语法时，
优先从内存模型、类型系统、编译期/运行期区别的角度进行解析和类比。

### 技术栈
- 前端：React 19（TypeScript 优先）
- 后端：Python（FastAPI / LiteLLM / 自研 Agent 框架）
- 目标：AI Agent Web 应用 + Electron 桌面客户端

### Python 解析原则（C++ 视角）
遇到复杂 Python 语法时，按以下框架解释：
1. **内存角度**：这个对象在 heap 还是 stack？引用计数 vs 手动管理？
2. **类型角度**：Python 的鸭子类型 ≈ C++ 的 concept/template，显式说明等价物
3. **执行角度**：装饰器 ≈ 编译期宏/模板元编程；生成器 ≈ 协程/lazy evaluation
4. **GIL 角度**：涉及并发时，必须说明 GIL 限制 vs C++ 真正的多线程差异

### 代码引用规范
- 单行引用：`src/memory/MemoryManager.ts:42`
- 范围引用：`src/memory/MemoryManager.ts:38-65`
- 跨文件关联：主文件行号 → 所有被引用文件行号，逐一列出
- 涉及函数时：同时给出定义位置和主要调用位置
- **禁止使用"在某个文件里"等模糊描述**

### 输出规范
- 所有回答使用中文
- 专有名词、行业通用缩写保留英文（LLM、RAG、token、runtime、heap）
- 代码注释也用中文
- 给出解释时，优先用 C++ 类比，再说 Python 实现
- 涉及性能分析时，附上时间/空间复杂度

### 禁止行为
- 不要省略错误处理
- 不要给出没有类型注解的 Python 函数
- 不要在不解释 GIL 影响的情况下推荐 threading 方案
- 不要使用模糊路径描述，必须给出精确的文件路径和行号

---

## Project Overview

WhatIf 是一款**小说转 AI 互动叙事游戏**引擎，分两个阶段工作：

1. **Preprocessing（提取）**：用 LLM 从中文小说中提取 WorldPkg（世界包）
2. **Runtime（游戏引擎）**：6 个专用 Agent 协作驱动叙事，支持 Web UI / CLI / Electron

---

## Common Commands

### 环境准备（首次）
```bash
python -m venv .venv
pip install -r backend/requirements.txt
python -m spacy download zh_core_web_sm
cd frontend && pnpm install
```

### 启动开发环境（全栈）
```bash
python start.py
# 后端 :8000，前端 :3030，Ctrl+C 同时停止
```

### 单独启动
```bash
# 后端（支持热重载）
cd backend && uvicorn api.app:app --reload --port 8000

# 前端
cd frontend && pnpm dev --port 3030
```

### 提取世界包
```bash
cd backend
python extract.py ../data/novels/novel.txt ../output/novel
```

### CLI 游玩
```bash
cd backend
python play.py <world_name>
```

### 前端构建与检查
```bash
cd frontend
pnpm build     # 生产构建
pnpm lint      # ESLint 检查
pnpm run electron:build  # 打包桌面应用
```

### 验证后端模块完整性
```bash
cd backend
python -c "import config; import core; import runtime; import api; print('All modules OK')"
```

---

## Architecture: Two-Phase System

### Phase 1 — Preprocessing Pipeline

```
Novel.txt
  → Text Segmentation (spaCy)
  → Event Extraction (LLM)          → events.json
  → Lorebook Extraction (LLM)       → characters/locations/items/knowledge.json
  → Decision Text Extraction (LLM)
  → Entity Transition Analysis:
      entity_scanner → necessity_grader → transition_annotator
      → cross_validator → repairer → batch_manager
  → WorldPkg.wpkg (zip)
```

入口：`backend/extract.py`
实体转换流水线：`backend/preprocessing/entity_transition/`

### Phase 2 — Runtime Game Engine

```
WorldPkg.wpkg
  → GameEngine (runtime/game.py)
  → AgentExecutor (agents/base.py) 协调 6 个 Agent
  → FastAPI SSE → Frontend / CLI / Electron
```

#### 6 个 Runtime Agent

| Agent | 目录 | 职责 |
|-------|------|------|
| NarrativeGenerationAgent | `agents/narrative_generation/` | 主叙事，三幕结构（setup/confrontation/resolution） |
| ContextEnrichmentAgent | `agents/context_enrichment/` | 历史召回 + 实体识别 + Lorebook 查询 |
| MemoryCompressionAgent | `agents/memory_compression/` | L0（短期）/ L1（长期）摘要，控制 token 消耗 |
| DeviationGuidanceAgent | `agents/deviation_guidance/` | 检测玩家偏离世界逻辑并软性引导 |
| SceneAdaptationAgent | `agents/scene_adaptation/` | 场景桥接，生成事件间过渡对话 |
| DeltaLifecycleAgent | `agents/delta_lifecycle/` | 管理"What If"平行时间线分支 |

Agent 框架基类：`backend/runtime/agents/base.py`（注册表模式，类似 C++ 工厂 + 虚函数表）

### LLM 多提供商抽象

`backend/core/llm.py` — `LLMClient`，底层使用 `litellm`：
- 统一接口，支持 DashScope/Qwen、Gemini、OpenAI、Anthropic、DeepSeek 等
- 每个 Agent / Extractor 的模型、temperature、thinking_budget 均在 `backend/llm_config.yaml` 中独立配置
- API Key 环境变量在 `backend/.env`（参考 `backend/.env.example`）

### API — SSE 流式推送

`backend/api/routes/game.py` 核心端点：

| Method | 路径 | 说明 |
|--------|------|------|
| POST | `/api/game/start` | 新游戏初始化 |
| POST | `/api/game/action` | 提交玩家行动（SSE 流） |
| POST | `/api/game/continue` | 继续等待中的叙事（SSE 流） |
| GET | `/api/game/state` | 当前游戏状态快照 |
| POST | `/api/game/save` / `load` | 存档管理 |
| PUT | `/api/config/llm` | 热更新 LLM 配置 |

SSE 事件类型：`chunk`（文本流）、`audio`（TTS）、`state`（游戏状态）、`error`、`done`

### 前端页面路由

`frontend/src/App.tsx` 管理页面切换：

| 页面 | 文件 | 功能 |
|------|------|------|
| Start | `pages/start-page.tsx` | 主菜单、选择世界包 |
| Gameplay | `pages/gameplay-page.tsx` | 游戏主界面（SSE 消费、存档） |
| Library | `pages/library-page.tsx` | 世界包浏览 |
| Settings | `pages/settings-page.tsx` | LLM 配置 & API Key 管理 |

SSE 客户端封装：`frontend/src/lib/api.ts`
本地配置持久化（Electron Store）：`frontend/src/lib/config-store.ts`

---

## Key Data Models

**提取阶段**（`backend/core/models.py`）：`Event`、`Character`、`Location`、`Item`、`Knowledge`、`Transition`、`Sentence`

**运行阶段**（`backend/runtime/agents/models.py`）：`EventContext`、`L0Summary`、`L1Summary`、`HistoryEntry`、`BridgeResult`

**API Schema**（`backend/api/schemas.py`）：`GameStateResponse`、`NarrativeResponse`、`EventInfo`

---

## Configuration

`backend/llm_config.yaml` — 统一配置所有 Extractor 和 Agent 的模型参数，修改后**无需重启**（通过 `/api/config/llm` PUT 热更新）。

`backend/config.py` — 校验 llm_config.yaml 完整性，定义输出路径（`output/`、`saves/`、`logs/`）和输出语言（`zh-CN` / `en`）。

---

## Session Logging & Debugging

- 日志位置：`logs/sessions/*.jsonl`（每行一个 JSON 事件）
- 可视化工具：`tools/log_analyzer.html`（拖入 JSONL 文件即可分析 LLM 调用链）
