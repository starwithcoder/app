"""基于 ChromaDB 的记忆仓储（实体级数据访问）。

职责分层：
- infrastructure.database.chromadb_client.BaseCollection 提供"通用 CRUD"（技术实现）
- 本模块定义"具体实体集合"，遵循 repositories → infrastructure 的单向依赖

包含：
- SessionCollection : 会话原始消息（memories_collection）
- SummaryCollection : 会话摘要（summary_collection）

后续 FactsCollection（原子事实库）也应加在这里。
"""
from typing import Any, Dict

from infrastructure.database.chromadb_client import BaseCollection, get_chromadb_client

# collection 名称常量（实体级，放在仓储层）
MEMORIES_COLLECTION = "memories_collection"
SUMMARY_COLLECTION = "summary_collection"


class SessionCollection(BaseCollection):
    """会话记忆集合（对应 memories_collection）。"""

    collection_name = MEMORIES_COLLECTION

    async def add_memory(self, user_id: str, session_id: str, message: Dict[str, Any]) -> bool:
        return await self.add(
            user_id=user_id,
            session_id=session_id,
            document=message.get("message"),
            role=message.get("role"),
        )


class SummaryCollection(BaseCollection):
    """会话摘要集合（对应 summary_collection）。"""

    collection_name = SUMMARY_COLLECTION

    async def add_summary(self, user_id: str, session_id: str, summary: str) -> bool:
        return await self.add(
            user_id=user_id,
            session_id=session_id,
            document=summary,
            role="summary",
        )


# ---- 全局实例（连接失败时为 None，调用方需判空）----
_client = get_chromadb_client().client
session_collection = SessionCollection(_client) if _client else None
summary_collection = SummaryCollection(_client) if _client else None
