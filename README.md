# Multi-Agent Conversational Memory System

> 一个具备**三层记忆**的多 Agent 对话系统：FastAPI + OpenAI Agents SDK + MCP + Redis + ChromaDB。

模型不仅能调用工具完成任务，还能**记住**用户的偏好与历史决策：短期记忆每轮自动注入，中长期记忆由模型**主动判断**是否需要召回（MemGPT / Letta 风格）。

---

## 特性

**多 Agent 与工具**
- **多 Agent 编排**：主调度（Orchestrator）+ 技术专家（Technical）+ 全能业务（Service）
- **MCP 工具集成**：搜索 / 百度地图等外部服务
- **流式响应**（SSE）

**三层记忆**
| 层 | 介质 | 说明 |
|---|---|---|
| 短期 | Redis | 未归档消息（pending），每轮**自动注入**上下文 |
| 中期 | ChromaDB | 叙事归档，模型按需 `search_past_conversations` / `recall_recent_sessions` |
| 长期 | ChromaDB | 原子事实库（跨会话、按 subject 去重更新）+ 滚动摘要 |

**关键设计**
- **写时批量落盘（write-behind）**：消息先入 Redis，扫描协程按水位线批量写 Chroma，不重复写
- **一次 LLM 调用产出摘要 + 事实**：结构化 JSON，省钱且上下文一致
- **增量摘要 + 超限压缩**：摘要滚太长会自动触发"摘要的摘要"
- **中文 embedding**：`BAAI/bge-m3`（1024 维），走硅基流动的 OpenAI 兼容接口（也支持切本地 Ollama）
- **模型自主检索**：4 个记忆工具注册为 `@function_tool`，由模型决定何时调用
- **会话管理**：后端统一创建会话（幂等）、首问自动命名、持久会话索引
- **软删除 + 导出**：用户点删除秒生效，后台协程补归档 → 导出到文件 → 真删，失败自动重试
- **并发安全**：per-session 分布式锁（Redis SET NX）、单调递增水位线（免疫时钟漂移）

---

## 架构概览

```
                    ┌──────────────┐
                    │   用户请求    │
                    └──────┬───────┘
                           ↓
           ┌───────────────────────────────┐
           │    Orchestrator Agent          │
           │                               │
           │  短期：直接注入（system+摘要+近期）│
           │  中长期：模型按需调 recall_* 工具  │
           └──┬───────────┬───────────┬────┘
              ↓           ↓           ↓
   业务工具          记忆检索工具
   (技术/服务站)     get_session_summary
                    recall_recent_sessions
                    search_past_conversations
                    recall_user_facts

后台常驻协程 Session Scanner（lifespan 启动，每 SCAN_INTERVAL 一轮）
   ① 消费删除队列：补归档 → 导出 → Chroma 真删 → 清 Redis → 出队
   ② 遍历活跃会话（per-session 锁）
       - flush：把水位线之后的新消息写进 Chroma
       - 摘要：新增 ≥ K 条 或 距上次 ≥ T 秒 → 一次 LLM 调用产出 {summary, facts[]}
       - 裁剪：pending 保留最近 N 条
       - 清理：空闲超 7 天 → 最终归档后移出活跃集合
```

---

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | FastAPI（async，lifespan 管理协程生命周期） |
| LLM 框架 | OpenAI Agents SDK |
| 主模型 | 硅基流动（推理） |
| 子模型 | 阿里百炼（通用 / 工具） |
| Embedding | `BAAI/bge-m3`（1024 维，硅基流动 API；可切本地 Ollama） |
| 短期存储 | Redis（建议开启 AOF） |
| 长期存储 | ChromaDB（向量库） |
| 外部工具 | MCP（搜索 / 百度地图） |
| 配置 | Pydantic Settings |

---

## 项目结构

```
app/
├── api/
│   ├── main.py                       # FastAPI 入口 + lifespan（启动/停止扫描协程）
│   └── routers.py                    # 路由：对话 / 会话列表 / 建会话 / 加载消息 / 删会话
├── config/
│   └── settings.py                   # 配置中心（密钥、Redis、embedding、四类上下限）
│
├── infrastructure/
│   ├── ai/                           # LLM 客户端、prompt 加载
│   ├── database/
│   │   ├── redis_client.py           # 同步 + 异步 Redis 客户端
│   │   └── chromadb_client.py        # BaseCollection + 中文 embedding 函数
│   ├── logging/
│   └── tools/                        # 本地工具 + MCP 客户端
│
├── multi_agent/
│   ├── orchestrator_agent.py         # 主调度 Agent（挂载 AGENT_TOOLS）
│   ├── technical_agent.py / service_agent.py
│   ├── agent_factory.py              # 业务工具 + 注册 AGENT_TOOLS
│   └── memory_tools.py               # ★ 4 个记忆检索 @function_tool + SessionContext
│
├── prompts/                          # Prompt 模板（含记忆工具使用原则）
│
├── repositories/                     # 数据访问
│   ├── redis_session_repository.py   # Redis：pending/摘要/meta/墓碑/锁/会话索引/删除队列
│   ├── memory_repository.py          # Chroma：Session/Summary/Facts 三个集合
│   └── session_repository.py         # JSON（遗留，仅会话列表还剩一点引用）
│
├── services/
│   ├── session_service.py            # 会话业务门面（准备上下文 / 记录消息 / 删除）
│   ├── session_memory_service.py     # ★ 记忆核心：上下文装配、摘要抽取、召回、删除
│   └── agent_service.py              # Agent 调用 + 流式输出
│
├── tasks/
│   └── session_scanner.py            # ★ 后台协程：归档 / 摘要 / 删除队列 / 清理
│
├── scripts/
│   └── migrate_embeddings.py         # 换 embedding 模型时的集合迁移脚本
│
├── exports/deleted/                  # 已删除会话的导出数据（含用户原文，已 gitignore）
├── agent_web_ui/                     # 前端（Vue 3 + Vite）
└── docs/                             # 设计文档
    ├── memory-module-design.md       # 记忆模块设计（决策记录）
    ├── 01-thinking-evolution.md      # 设计演进复盘
    ├── 02-project-readme.md          # 项目说明（内部版）
    └── 03-modern-agent-architecture-comparison.md   # 与现代 Agent 架构对比
```

---

## 快速开始

### 环境要求
- Python 3.11+
- Redis 7+（**建议开启 AOF**，否则重启会丢会话与水位线）
- ChromaDB 0.4+

### 安装
```bash
pip install -r requirements.txt
```

### 配置

编辑 `config/settings.py`，或写入 `.env`（Pydantic Settings 会自动读取）：

```python
# 模型
SF_API_KEY = "..."                      # 硅基流动（推理模型）
SF_BASE_URL = "https://api.siliconflow.cn/v1"
AL_BAILIAN_API_KEY = "..."              # 阿里百炼（通用模型）
AL_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SUB_MODEL_NAME = "..."

# Redis
REDIS_URL = "redis://localhost:6379/0"

# Embedding（默认 BAAI/bge-m3，1024 维）
EMBEDDING_MODEL = "BAAI/bge-m3"
# 想切本地 Ollama：
#   ollama pull bge-m3
#   EMBEDDING_BASE_URL = "http://localhost:11434/v1"
#   EMBEDDING_API_KEY  = "ollama"
#   （并把三个 *_COLLECTION 名字换成 *_v3 后跑迁移脚本）
```

**四类上下限**（都有默认值，按需调整）：
```
① 用户输入  MAX_MESSAGE_CHARS / SHORT_TERM_KEEP / HISTORY_LOAD_LIMIT
② 摘要      SUMMARY_MIN_NEW / SUMMARY_MAX_INTERVAL / SUMMARY_MAX_CHARS
③ 送入模型  CONTEXT_MIN_RECENT / CONTEXT_MAX_RECENT / CONTEXT_MAX_TOKENS
④ 模型输出  MAX_OUTPUT_TOKENS / MAX_REPLY_CHARS
```

### 运行

```bash
# 后端（main.py 是工厂函数 create_fast_api，需加 --factory）
uvicorn api.main:create_fast_api --factory --reload --host 127.0.0.1 --port 8000

# 前端
cd agent_web_ui && npm run dev
```

后台 `Session Scanner` 协程随 FastAPI `lifespan` 自动启动/停止。

### 测试

```bash
pytest -v                      # 全部用例（22 项）
pytest -k lock                 # 只跑名字含 lock 的用例
pytest tests/test_session_scanner.py    # 只跑某个文件
```

用例位于 `tests/`，是**集成测试**：连真实 Redis，但用 stub 替代 LLM 与 Chroma，
不产生真实模型调用、不写向量库。Redis 不可用时整体 skip（不判失败）。

| 文件 | 覆盖 |
|---|---|
| `test_redis_session_repository.py` | pending / summary / 摘要水位线 / 墓碑 / per-session 锁 / 活跃 zset / 裁剪 / 清理 |
| `test_session_scanner.py` | 落库、摘要、事实写入、水位线幂等、未达阈值只落库不摘要 |
| `test_session_memory_service.py` | 上下文装配、摘要注入、墓碑过滤、摘要 JSON 解析、中期召回 |

---

## 核心流程

```
1. 点击"新会话"
   → POST /api/session/create → 后端生成 session_id 并登记索引（幂等）

2. 发送消息
   → prepare_history：读 [system] + [滚动摘要] + [pending 最近 K 条] + [本次输入]
   → record_user_message：先把用户消息落库（Agent 还没跑就存好，不怕断流）
   → Agent 运行（可主动调 recall_* 工具）
   → record_assistant_message：补存助手回复
   → onFinished → 刷新会话列表（会话名从"新对话"变成首问）

3. 加载会话
   → POST /api/session/messages → 返回该会话正文（列表接口只给元信息，正文按需加载）

4. 删除会话
   → POST /api/session/delete → 软删（列表立刻消失）+ 入队
   → 后台协程：补归档 → 导出到 exports/deleted/ → Chroma 真删 → 清 Redis → 出队（失败重试）

5. 后台扫描（每 SCAN_INTERVAL）
   → 消费删除队列 → 遍历活跃会话做 flush / 摘要 / 裁剪 / 清理
```

---

## 记忆模型

### 三层分工

| 层 | 介质 | 内容 | 使用方式 |
|---|---|---|---|
| 短期 | Redis | pending 未归档消息 | 每轮自动注入 |
| 中期 | ChromaDB | 叙事归档 | 模型按需 `search_past_conversations` / `recall_recent_sessions` |
| 长期 | ChromaDB | 原子事实（跨会话、按 subject upsert） | 模型按需 `recall_user_facts` |
| 长期 | Redis | 滚动摘要（TTL 7 天） | 模型按需 `get_session_summary`，也随短期注入 |

### 水位线（防止重复写 / 支持增量摘要）

| 水位线 | 记什么 |
|---|---|
| `flushed_seq` | 已写进 Chroma 到哪条 → 归档不重复 |
| `summarized_seq` | 已摘要到哪条 → 增量摘要、不重算历史 |

用**单调递增序号**而非时间戳，天然免疫多机时钟漂移。

### 触发条件（**不判断用户是否活跃**）

| 动作 | 触发 |
|---|---|
| Flush pending → Chroma | 每轮扫描都做（只写水位线之后的新消息） |
| 摘要 + 事实抽取 | 距上次新增 ≥ `SUMMARY_MIN_NEW` 条 **或** 距上次 ≥ `SUMMARY_MAX_INTERVAL` 秒 |
| 活跃 zset 清理 | idle > 7 天（最终归档后移出） |

### 摘要 LLM 调用契约

单次调用，输入 `old_summary + 新增消息`，输出：

```json
{
  "summary": "滚动摘要",
  "facts": [
    {
      "category": "decision | preference | project | people | task",
      "subject": "事实主语（用于 upsert 去重）",
      "content": "事实正文",
      "entities": ["实体1", "实体2"]
    }
  ]
}
```

### 删除策略

用户看到的"删除" ≠ 后端真删，分三层：

| 阶段 | 做什么 |
|---|---|
| 软删除（用户触发） | 移出会话索引 → 前端立刻看不到 |
| 导出（后台） | 完整数据写进 `exports/deleted/` 供分析 |
| 真删（后台） | Chroma 与 Redis 物理删除 |

---

## 路线图

### 已完成
- [x] 多 Agent 编排 + MCP 工具 + 流式响应
- [x] 三层记忆（Redis + ChromaDB）
- [x] 4 个记忆工具接入 Agents SDK
- [x] 中文 embedding（bge-m3）+ 集合迁移脚本
- [x] 会话管理（后端建会话、首问命名、持久索引）
- [x] 删除会话（软删 + 导出 + 后台真删）
- [x] 四类上下限配置化（含 token 预算、摘要压缩）
- [x] pytest 化测试（`tests/`，22 项，含锁/水位线/墓碑/幂等）

### 进行中 / 待办
- [ ] 清洗旧集合（`python scripts/migrate_embeddings.py --drop-old`）
- [ ] 老 JSON 会话迁移进 Redis 索引
- [ ] 完整历史分页加载（当前展示最近 `HISTORY_LOAD_LIMIT` 条）
- [ ] 监控埋点 / Tracing
- [ ] Agent 评估集（开/关记忆的效果对比）

---

## 许可证

[MIT](./LICENSE)（待添加）
