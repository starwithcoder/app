"""记忆检索工具（供 Orchestrator Agent 按需调用）。

职责：把 services/session_memory_service 的中长期召回能力包装成
OpenAI Agents SDK 的 @function_tool，让**模型自己判断**要不要查记忆
（而不是我们在代码里硬编码注入）。

上下文注入链路：
    services/agent_service.py
        Runner.run_streamed(..., context=SessionContext(user_id, session_id))
            → 本模块各工具通过 ctx.context 取到 user_id / session_id

注意：工具函数签名里第一个参数必须是 RunContextWrapper，
      SDK 会自动注入；其余参数会生成给模型看的 JSON Schema，
      因此 docstring 要写清楚"什么时候该用"。
"""
from dataclasses import dataclass

from agents import RunContextWrapper, function_tool

from infrastructure.logging.logger import logger
from services.session_memory_service import session_memory_service


@dataclass
class SessionContext:
    """Agent 运行时上下文：记忆工具靠它知道该查哪个用户 / 会话。"""
    user_id: str = ""
    session_id: str = ""


@function_tool
async def get_session_summary(ctx: RunContextWrapper[SessionContext]) -> str:
    """【本会话摘要】获取当前会话此前对话的滚动摘要。

    当用户提到"刚才我们说了什么"、"之前聊到哪了"、"总结一下我们的对话"时调用。
    只返回摘要文本，不含本轮新消息（本轮内容你自己已经能看到）。
    """
    try:
        summary = await session_memory_service.get_summary(
            ctx.context.user_id, ctx.context.session_id
        )
        return summary or "（本会话暂无历史摘要）"
    except Exception as e:
        logger.exception("get_session_summary 调用失败")
        return f"读取本会话摘要失败: {e}"


@function_tool
async def recall_recent_sessions(ctx: RunContextWrapper[SessionContext], window: int = 3) -> str:
    """【近期会话】获取该用户最近若干个历史会话的摘要。

    当用户提到"我之前问过"、"上次那个项目"、"之前聊过 XX"时调用。

    Args:
        window: 取最近几个会话（默认 3）。
    """
    try:
        items = await session_memory_service.recall_recent(
            ctx.context.user_id, window=window
        )
        if not items:
            return "（暂无历史会话摘要）"
        return "\n\n".join(f"· 会话 {it['session_id']}：{it['summary']}" for it in items)
    except Exception as e:
        logger.exception("recall_recent_sessions 调用失败")
        return f"读取近期会话失败: {e}"


@function_tool
async def search_past_conversations(
    ctx: RunContextWrapper[SessionContext], query: str, top_k: int = 5
) -> str:
    """【语义检索历史对话】在历史消息中做语义检索，找回具体细节。

    当需要回忆**具体内容**（某个数值、某句原话、某个操作步骤）时使用，
    比摘要更精确。

    Args:
        query: 用自然语言描述你要找的内容。
        top_k: 返回条数（默认 5）。
    """
    try:
        hits = await session_memory_service.recall_search(
            ctx.context.user_id, ctx.context.session_id, query, top_k=top_k
        )
        if not hits:
            return f"（未检索到与“{query}”相关的历史对话）"
        return "\n".join(f"- {h.get('content', '')}" for h in hits)
    except Exception as e:
        logger.exception("search_past_conversations 调用失败")
        return f"检索历史对话失败: {e}"


@function_tool
async def recall_user_facts(
    ctx: RunContextWrapper[SessionContext], query: str, category: str = ""
) -> str:
    """【长期事实】检索该用户的长期事实（偏好 / 决策 / 项目 / 人物 / 任务）。

    当用户的问题依赖他的**个人偏好、既往决定或项目背景**时调用，
    例如"我喜欢用什么数据库"、"上次我们定的是哪个方案"。

    Args:
        query: 要查的事实关键词或自然语言问题。
        category: 可选，限定类别（decision / preference / project / people / task），
                  留空表示不限。
    """
    try:
        facts = await session_memory_service.recall_facts(
            ctx.context.user_id, query, category=category or None
        )
        if not facts:
            return f"（未检索到与“{query}”相关的长期事实）"
        lines = []
        for item in facts:
            if isinstance(item, dict):
                lines.append(
                    f"- [{item.get('category', '')}] "
                    f"{item.get('subject', '')}：{item.get('content', '')}"
                )
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("recall_user_facts 调用失败")
        return f"检索长期事实失败: {e}"


# 暴露给 multi_agent/agent_factory.py 注册进 AGENT_TOOLS
MEMORY_TOOLS = [
    get_session_summary,
    recall_recent_sessions,
    search_past_conversations,
    recall_user_facts,
]
