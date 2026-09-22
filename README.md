# Multi-Agent Conversational Memory System

> 一个具备**分层长期记忆**的多 Agent 对话系统：FastAPI + OpenAI Agents SDK + MCP + Redis + ChromaDB。

---

## 特性

- **多 Agent 编排**：主调度（Orchestrator）+ 技术专家（Technical）+ 全能业务（Service），通过 Agents SDK 工具路由
- **MCP 工具集成**：搜索 / 百度地图等外部服务（Model Context Protocol）
- **三层记忆模型**：
  - **短期**：Redis pending，最近未归档消息，永远注入上下文
  - **中期**：ChromaDB 叙事归档，模型按需调用 `recall_recent` / `recall_search`
  - **长期**：ChromaDB 原子事实库 + 滚动摘要，模型按需调用 `recall_facts` / `get_summary`
- **写时批量落盘（write-behind）**：消息先入 Redis，扫描协程批量写 Chroma，降低 embedding 成本
- **单次 LLM 调用同时产出摘要 + 事实**：结构化 JSON 输出，省钱且上下文一致
- **墓碑式删除**：删除即追加墓碑 + `deleted_ids` 集合，可逆且不破坏历史
- **模型自主检索**：短期由系统注入，中长期由模型按需调用工具（MemGPT / Letta 风格）
- **并发与持久化**：per-session 锁、Redis AOF、Chroma 关闭释放

---

## 架构概览

```
                     ┌──────────────┐
                     │   用户请求    │
                     └──────┬───────┘
                            ↓
            ┌─────────────────────────────┐
            │    Orchestrator Agent        │
            │    (sub_model: 阿里百炼)      │
            │                             │
            │  - 短期记忆直接注入上下文       │
            │  - recall_* 工具按需调用       │
            └──┬─────────────────┬────────┘
               ↓                 ↓
   consult_technical_expert   query_service_station_and_navigate
               ↓                 ↓
          Technical Agent     Service Agent
               ↓                 ↓
           MCP servers (search / baidu)

后端后台：
   ┌──────────────────────────────────┐
   │   Session Scanner 协程            │
   │   (lifespan 启动)                  │
   │                                   │
   │   - 定时 flush pending → Chroma    │
   │   - 条件触发 LLM 摘要 + 事实抽取   │
   │   - idle 极长时清理活跃 zset       │
   └──────────────────────────────────┘
```

---

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | FastAPI |
| LLM 框架 | OpenAI Agents SDK |
| 主模型 | 硅基流动（推理） |
| 子模型 | 阿里百炼（通用 / 工具） |
| 短期存储 | Redis（建议开启 AOF） |
| 长期存储 | ChromaDB（向量数据库） |
| 外部工具 | MCP（搜索 / 百度地图） |
| 日志 | 自定义 logger |
| 配置 | Pydantic Settings |

---

## 项目结构

```
app/
├── api/                              # FastAPI 路由层
├── config/
│   └── settings.py                   # 配置中心（API Key、Redis、Chroma 等）
│
├── infrastructure/                   # 基础设施
│   ├── ai/
│   │   ├── openai_client.py          # 主/子模型客户端
│   │   └── prompt_loader.py
│   ├── database/
│   │   └── chromadb_client.py        # ChromaDB 客户端（BaseCollection + 子类）
│   ├── logging/
│   │   └── logger.py
│   └── tools/
│       ├── local/                    # 本地工具
│       └── mcp/                      # MCP 客户端
│
├── multi_agent/                      # 多 Agent 定义
│   ├── orchestrator_agent.py         # 主调度 Agent（挂载 AGENT_TOOLS）
│   ├── technical_agent.py            # 技术专家
│   ├── service_agent.py              # 业务专家
│   └── agent_factory.py              # @function_tool 注册
│
├── prompts/                          # Prompt 模板
│
├── repositories/                     # 数据仓储
│   ├── session_repository.py         # JSON 文件（待替换）
│   └── redis_session_repository.py   # Redis CRUD（pending/summary/zset/lock）
│
├── schemas/                          # Pydantic schemas
│
├── services/                         # 业务服务
│   ├── session_service.py            # 准备/保存历史
│   ├── session_memory_service.py     # 三层记忆装配 + 摘要/抽取
│   └── stream_response_service.py    # 流式响应
│
├── tasks/
│   └── session_scanner.py            # lifespan 协程（flush / 摘要 / 清理）
│
├── tests/                            # 测试
│
├── user_memories/                    # JSON 历史（本地数据，已 gitignore）
├── utils/                            # 工具函数
│
└── README.md                         # 本文件
```

---

## 快速开始

### 环境要求
- Python 3.11+
- Redis 7+（建议开启 AOF）
- ChromaDB 0.4+

### 安装

```bash
pip install -r requirements.txt
```

### 配置

在 `config/settings.py` 中配置：

```python
# 硅基流动（主模型 / 推理）
SF_API_KEY = "your-siliconflow-api-key"
SF_BASE_URL = "https://api.siliconflow.cn/v1"
MAIN_MODEL_NAME = "your-main-model"

# 阿里百炼（子模型 / 通用）
AL_BAILIAN_API_KEY = "your-bailian-api-key"
AL_BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SUB_MODEL_NAME = "your-sub-model"

# Redis
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_DB = 0

# ChromaDB 存储路径
CHROMA_PATH = "./chroma_data"
```

> **建议**：生产环境将密钥移到环境变量，参考 `.env.example`（自行创建）。

### 运行

```bash
uvicorn api.main:app --reload
```

后台 `Session Scanner` 协程会随 FastAPI `lifespan` 自动启动。

---

## 核心流程示例

```
1. 用户发送消息
   └─→ api 接收 → SessionService.prepare_history()
       └─→ session_memory_service.prepare_context()
           ├─→ redis 读 pending 最近 K 条
           ├─→ redis 读 summary
           └─→ 拼装 [system] + [summary] + [pending] + [user_input]

2. Orchestrator Agent 运行
   ├─→ 必要时调 recall_recent / recall_facts / get_summary
   └─→ 必要时调 consult_technical_expert / query_service_station_and_navigate

3. 响应返回后
   └─→ SessionService.save_history()
       └─→ session_memory_service.record_message()
           ├─→ RPUSH chat:pending:{u}:{s}
           ├─→ ZADD chat:active_sessions (刷新活跃时间)
           └─→ 记录 user + assistant 两条消息

4. 后台 Session Scanner（每 30s）
   └─→ 扫描 chat:active_sessions
       ├─→ 若 pending 非空 → flush 到 Chroma
       ├─→ 若新增 ≥ K 条 或 距上次 ≥ T → 调 LLM 摘要 + 事实抽取
       │   └─→ 输出 {summary, facts[]} → 写回 redis + Chroma
       └─→ idle > 7 天 → 从 zset 移除
```

---

## 记忆模型

### 三层分工

| 层 | 介质 | 内容 | 注入方式 |
|---|---|---|---|
| 短期 | Redis | pending 未归档消息 | 永远注入 |
| 中期 | ChromaDB | 叙事归档 / 历史会话 | 模型按需 `recall_recent` / `recall_search` |
| 长期 | ChromaDB | 原子事实（按 subject upsert） | 模型按需 `recall_facts` |
| 长期 | Redis | 滚动摘要文本 | 模型按需 `get_summary` |

### 触发条件（不判活跃）

| 动作 | 触发 |
|---|---|
| Flush pending → Chroma | 定时（每 30s）或 pending 长度 ≥ M |
| 摘要 + 事实抽取 | 距上次新增 ≥ K 条 **或** 距上次 ≥ T 时间 |
| 活跃 zset 清理 | idle > 长阈值（如 7 天） |

### 摘要 LLM 调用契约

单次调用，输入 `old_summary + 新增消息`，输出：

```json
{
  "summary": "滚动摘要（受 token 上限截断）",
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

---

## 路线图

### v1（当前）
- [x] 多 Agent 编排（Orchestrator + Technical + Service）
- [x] MCP 工具集成
- [x] 流式响应
- [x] JSON 文件会话存储（遗留，待替换）

### v2（设计中）
- [ ] 三层记忆模块落地（Redis + ChromaDB）
- [ ] `redis_session_repository` / `session_memory_service` / `session_scanner` 三件套
- [ ] Agents SDK 记忆工具接入
- [ ] `FactsCollection` 实现

### v3+
- [ ] Tracing（step-level reasoning trace）
- [ ] Human-in-the-loop（敏感操作确认）
- [ ] 监控埋点（pending 长度、flush 成功率、摘要耗时）
- [ ] 多模态（图像 / 语音 / 文件）
- [ ] Agent 评估集（golden dataset）

---

## 贡献

欢迎提交 Issue 与 Pull Request。

---

## 许可证

[MIT](./LICENSE)（待添加）