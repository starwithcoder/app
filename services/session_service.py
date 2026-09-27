"""
会话业务管理服务类

主要职责：
1. 准备历史对话（Agent 运行前）：装配短期记忆上下文
   [system] + [滚动摘要] + [pending 最近 N 条] + [本次输入]
2. 记录用户消息（Agent **运行前**）：提前落库，避免流式中断丢消息
3. 记录助手回复（Agent **运行后**）：补上本轮助手的回复
4. 创建会话 / 查询会话列表（前端侧边栏）

存储说明：
- 短期记忆与会话索引都在 Redis（见 services/session_memory_service.py）
- 老的 JSON 会话文件尚未迁移进 Redis 索引
"""
from typing import Any, Dict, List, Optional

from config.settings import settings
from infrastructure.logging.logger import logger
from repositories.session_repository import session_repository
from services.session_memory_service import session_memory_service


class SessionService:
    """
    会话业务管理服务类
    """

    DEFAULT_SESSION_ID = "default_session"

    def __init__(self):
        """初始化会话操作的工具。"""
        self._repo = session_repository
        self._memory = session_memory_service

    async def prepare_history(
            self, user_id: str, session_id: Optional[str], user_input: str, max_turn: int = 3
    ) -> List[Dict[str, Any]]:
        """
        装配短期记忆上下文（Agent 运行前调用）。

        结构：[system] + [滚动摘要] + [pending 最近 max_turn*2 条] + [本次用户输入]

        Args:
            user_id: 用户id
            session_id: 会话id
            user_input: 当前用户输入
            max_turn: 送入 LLM 的最大轮数（默认最近 3 轮 = 6 条消息）

        Returns:
            List[Dict[str, Any]]: Agent 输入消息列表

        注意：本方法只读，不写消息。
        写入请用 record_user_message / record_assistant_message。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        return await self._memory.prepare_context(
            user_id=user_id,
            session_id=target_session_id,
            user_input=user_input,
            recent_k=max_turn * 2,
        )

    async def record_user_message(
        self, user_id: str, session_id: Optional[str], user_input: str
    ) -> Optional[int]:
        """记录用户消息（**Agent 运行前**调用）。

        提前落库（而不是等模型答完）的好处：
        - 即使流式中断 / 客户端断开，这一轮也不会丢
        - 会话立刻进入持久索引，侧边栏马上能看到
        - 首问自动命名立刻生效

        Returns:
            Optional[int]: 消息 id。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        return await self._memory.record_message(
            user_id=user_id,
            session_id=target_session_id,
            role="user",
            content=user_input,
            max_chars=settings.MAX_MESSAGE_CHARS,   # 上限：超长输入截断后入库
        )

    async def record_assistant_message(
        self, user_id: str, session_id: Optional[str], assistant_reply: str
    ) -> Optional[int]:
        """记录助手回复（**Agent 运行后**调用）。

        用户消息请在运行前用 record_user_message 记录，这里只补助手这一条。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        return await self._memory.record_message(
            user_id=user_id,
            session_id=target_session_id,
            role="assistant",
            content=assistant_reply,
            max_chars=settings.MAX_REPLY_CHARS,     # 上限：超长回复截断后入库
        )

    async def create_session(
        self, user_id: str, session_id: Optional[str] = None
    ) -> Optional[str]:
        """创建新会话（前端点击"新会话"时调用）。

        Returns:
            Optional[str]: 新建的 session_id，前端拿它缓存并继续发消息。
        """
        return await self._memory.create_session(user_id, session_id)

    async def delete_session(self, user_id: str, session_id: Optional[str]) -> bool:
        """删除会话：**只做软删除并入队，立即返回**。

        真实清理（把未归档消息补进 Chroma → 导出 → 真删 → 清 Redis）由后台协程完成。
        这样做的好处：
        - 用户点删除秒生效（列表里立刻消失）
        - 后台失败可重试，不会留下"Redis 清了但 Chroma 还在"的半删状态

        Returns:
            bool: 是否成功标记并入队。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        return await self._memory.request_delete(user_id, target_session_id)

    async def get_session_messages(
        self, user_id: str, session_id: Optional[str], limit: int = settings.HISTORY_LOAD_LIMIT
    ) -> List[Dict[str, str]]:
        """加载某个会话的消息记录（前端点开会话时调用）。

        注意：会话**列表**接口只返回元信息（不含正文），
        正文要靠这个接口按需加载，避免列表请求把全部聊天内容都拉回来。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        return await self._memory.get_session_messages(
            user_id=user_id, session_id=target_session_id, limit=limit
        )

    async def get_all_sessions_memory(self, user_id: str) -> List[Dict[str, Any]]:
        """获取用户的所有会话列表（前端侧边栏）。

        数据源已从 JSON 文件切到 Redis：**持久会话索引 + 每个会话的 meta**，
        只返回元信息，**不加载消息正文**（点开会话时才加载）。

        Args:
            user_id: 用户唯一标识。

        Returns:
            List[Dict]: 按最近活跃时间倒序，每项含
                        session_id / title / create_time / memory / total_messages
                        （memory 恒为空数组，保留该字段仅为兼容前端）
        """
        sessions = await self._memory.list_sessions(user_id)
        logger.info(f"获取用户 {user_id} 的会话列表，共 {len(sessions)} 个")

        # 兼容旧前端字段：正文不再随列表返回
        for item in sessions:
            item["memory"] = []
        return sessions

# 全局单例
session_service = SessionService()