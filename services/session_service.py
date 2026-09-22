"""
会话业务管理服务类

主要职责：
1. 准备历史对话（Agent 运行前）：加载 → 拼当前用户输入 → 裁剪保留最近 N 轮
2. 保存历史对话（Agent 运行后）：把完整 chat_history 写回 JSON 文件
3. 查询用户全部会话列表（前端侧边栏）
"""
from typing import Any, Dict, List

from infrastructure.logging.logger import logger
from repositories.session_repository import session_repository


class SessionService:
    """
    会话业务管理服务类
    """

    DEFAULT_SESSION_ID = "default_session"

    def __init__(self):
        """初始化会话操作的工具。"""
        self._repo = session_repository

    def prepare_history(
            self, user_id: str, session_id: str, user_input: str, max_turn: int = 3
    ) -> List[Dict[str, Any]]:
        """
        准备历史会话: 加载会话--->拼接当前输入--->裁剪保留最近 N 轮。
        调用的时机：发送请求给 LLM 之前（Agent 运行之前）。

        Args:
            user_id: 用户id
            session_id: 会话id
            user_input: 当前用户输入
            max_turn: 送入 LLM 的最大轮数（默认最近 3 轮 = 6 条消息）

        Returns:
            List[Dict[str, Any]]: Agent 输入消息列表（含 system + 裁剪后的历史 + 当前输入）
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID

        # 1. 加载历史消息（首次会话为空 -> 初始化 system 指令）
        chat_history = self._repo.load_session(user_id, target_session_id)
        if chat_history is None:
            chat_history = [self._init_system_msg_instruct(target_session_id)]

        # 2. 拼接当前用户输入（用于本次 LLM 输入）
        chat_history = chat_history + [{"role": "user", "content": user_input}]

        # 3. 裁剪保留 system 消息 + 最近 max_turn 轮
        return self._truncate_history(chat_history, max_turn=max_turn)

    def save_history(
            self, user_id: str, session_id: str, chat_history: List[Dict[str, Any]]
    ) -> None:
        """
        保存完整历史对话。
        调用的时机：调用完 LLM（Agent）之后。

        Args:
            user_id: 用户id
            session_id: 会话id
            chat_history: 完整的对话历史（已包含 system + 全部 user/assistant 消息）
        """
        target_session_id = session_id if session_id else self.DEFAULT_SESSION_ID
        self._repo.save_session(user_id, target_session_id, chat_history)

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
            session_item = {
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

    def _truncate_history(
            self, chat_history: List[Dict[str, Any]], max_turn: int = 3
    ) -> List[Dict[str, Any]]:
        """裁剪保留最近 max_turn 轮对话（system 消息保留）。"""
        # 1. 获取系统角色的消息[无论如何都要留，通常来说就一条]
        system_msg = [msg for msg in chat_history if msg.get('role') == 'system']
        # 2. 获取非系统角色的消息【user & assistant】
        no_system_msg = [msg for msg in chat_history if msg.get('role') != 'system']
        msg_limit = max_turn * 2
        # 简单切片
        truncate_msg = no_system_msg[-msg_limit:]
        return system_msg + truncate_msg

    @staticmethod
    def _init_system_msg_instruct(session_id: str) -> Dict[str, Any]:
        """初始化一个带系统角色的消息结构。"""
        return {
            "role": "system",
            "content": f"你是一个有记忆的智能体助手，请基于上下文历史会话用户问题 (会话ID {session_id})"
        }


# 全局单例
session_service = SessionService()