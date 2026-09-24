"""
会话业务管理服务类

主要职责：
1. 准备历史对话（Agent 运行前）：装配短期记忆上下文
   [system] + [滚动摘要] + [pending 最近 N 条] + [本次输入]
2. 保存历史对话（Agent 运行后）：把本轮 user + assistant 追加到 Redis 短期记忆
3. 查询用户全部会话列表（前端侧边栏）

存储说明：
- 短期记忆已切到 Redis（见 services/session_memory_service.py）
- get_all_sessions_memory 仍读 JSON 文件（待迁移到 Redis）
"""
from typing import Any, Dict, List

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
            self, user_id: str, session_id: str, user_input: str, max_turn: int = 3
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

        注意：本方法只读，不写消息（写入由 save_history 负责）。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        return await self._memory.prepare_context(
            user_id=user_id,
            session_id=target_session_id,
            user_input=user_input,
            recent_k=max_turn * 2,
        )

    async def save_history(
            self, user_id: str, session_id: str, user_query: str, assistant_reply: str
    ) -> None:
        """
        保存本轮对话（Agent 运行后调用）。

        Args:
            user_id: 用户id
            session_id: 会话id
            user_query: 本轮用户输入
            assistant_reply: 本轮助手回复

        注意：只追加本轮新增的两条消息（pending 是追加式的），
              不要传整份 chat_history，否则会重复写入。
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        await self._memory.record_turn(
            user_id=user_id,
            session_id=target_session_id,
            user_input=user_query,
            assistant_reply=assistant_reply,
        )

    def get_all_sessions_memory(self, user_id: str) -> List[Dict[str, Any]]:
        """获取并格式化用户的所有会话列表（用于前端侧边栏展示）。

        Args:
            user_id: 用户唯一标识。

        Returns:
            List[Dict]: 按时间倒序排列的会话列表，
                        每项含 session_id / create_time / memory / total_messages。
        """
        # 1. 从 Repo 获取原始元数据
        # 类型提示: List[Tuple[session_id, create_time, data_or_error]]
        raw_sessions = self._repo.get_all_sessions_metadata(user_id)

        formatted_sessions = []
        for session_id, create_time, data_or_error in raw_sessions:
            session_item: Dict[str, Any] = {
                "session_id": session_id,
                "create_time": create_time,
            }

            # 2. 处理可能的读取错误（隔离异常，防止一个文件损坏导致整个列表挂掉）
            if isinstance(data_or_error, Exception):
                logger.error("读取会话 %s 失败: %s", session_id, str(data_or_error))
                session_item.update({
                    "memory": [],
                    "total_messages": 0,
                    "error": "无法读取会话数据",
                })
            else:
                # 3. 过滤掉 system 消息（侧边栏只展示 user/assistant）
                memory = [
                    msg for msg in (data_or_error or [])
                    if isinstance(msg, dict) and msg.get("role") != "system"
                ]
                session_item.update({
                    "memory": memory,
                    "total_messages": len(memory),
                })

            formatted_sessions.append(session_item)

        # 4. 排序：按时间倒序（最新的在最前）
        formatted_sessions.sort(
            key=lambda x: x.get("create_time") or "",
            reverse=True
        )
        return formatted_sessions

# 全局单例
session_service = SessionService()