import asyncio
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import chromadb

from infrastructure.logging.logger import logger


class BaseCollection:
    """封装一个 chromadb collection 的通用 CRUD（按 user_id + session_id 隔离）。

    只负责"技术实现"，不含任何业务实体语义；具体集合在 repositories 层定义。
    """

    collection_name: str = ""  # 子类必须覆盖

    def __init__(self, client: Any):
        if not self.collection_name:
            raise ValueError("子类必须定义 collection_name")
        self._client: Any = client
        self._collection: Any = None  # 懒加载并缓存

    @property
    def collection(self):
        if self._collection is None:
            self._collection = self._client.get_or_create_collection(name=self.collection_name)
        return self._collection

    async def add(
        self,
        user_id: str,
        session_id: str,
        document: Optional[str],
        role: Optional[str] = None,
    ) -> bool:
        if document is None or document == "":
            logger.warning("add: 文档为空，已忽略")
            return False

        unique_id = f"{user_id}_{session_id}_{uuid.uuid4().hex}"
        timestamp = int(time.time())
        try:
            await asyncio.to_thread(
                self.collection.add,
                ids=[unique_id],
                documents=[document],
                metadatas=[{
                    "user_id": str(user_id),
                    "session_id": str(session_id),
                    "timestamp": timestamp,
                    "role": str(role) if role is not None else "",
                }],
            )
            return True
        except Exception:
            logger.exception(f"add: 向集合 {self.collection_name} 添加失败")
            return False

    async def query(
        self,
        user_id: str,
        session_id: str,
        question: str,
        n_results: int = 5,
    ) -> Optional[Dict[str, Any]]:
        try:
            results = await asyncio.to_thread(
                self.collection.query,
                query_texts=[question],
                where={"$and": [{"user_id": str(user_id)}, {"session_id": str(session_id)}]},
                n_results=n_results,
            )
            return results
        except Exception:
            logger.exception(f"query: 查询集合 {self.collection_name} 失败")
            return None

    async def all(self, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            results = await asyncio.to_thread(
                self.collection.get,
                where={"$and": [{"user_id": str(user_id)}, {"session_id": str(session_id)}]},
                include=["documents", "metadatas"],
            )
            return results
        except Exception:
            logger.exception(f"all: 获取集合 {self.collection_name} 全部失败")
            return None

    async def newest(self, all_results: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """从 all() 的结果中取最新一条（按 timestamp 降序），返回与 get() 同构的字典。"""
        metadatas = (all_results or {}).get("metadatas") or []
        if not metadatas:
            logger.warning("newest: metadatas 为空，无法取最新")
            return None
        try:
            idx, _ = max(enumerate(metadatas), key=lambda item: item[1]["timestamp"])
        except (KeyError, TypeError):
            logger.exception("newest: metadata 缺少 timestamp 字段")
            return None

        ids = (all_results or {}).get("ids") or []
        documents = (all_results or {}).get("documents") or []
        return {
            "ids": [ids[idx]] if idx < len(ids) else [],
            "documents": [documents[idx]] if idx < len(documents) else [],
            "metadatas": [metadatas[idx]],
        }


class ChromadbClient:
    """管理 chromadb PersistentClient 的生命周期。

    只负责连接 / 关闭 / 对外暴露底层 client；
    具体集合（会话、摘要、事实）由 repositories 层基于此 client 构造。
    """

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._client = self.connect()

    @property
    def client(self) -> Any:
        """底层 chromadb 客户端（供 repositories 层构造具体集合）。"""
        return self._client

    def connect(self) -> Any:
        try:
            if not self.file_path.exists():
                self.file_path.mkdir(parents=True, exist_ok=True)
            return chromadb.PersistentClient(path=str(self.file_path))
        except Exception:
            logger.exception("connect: chromadb 连接失败")
            return None

    def close(self) -> None:
        """显式关闭底层连接（SQLite 文件锁），建议在应用退出时调用。"""
        if self._client is not None:
            self._client.close()


@lru_cache(maxsize=1)
def get_chromadb_client() -> ChromadbClient:
    base_url = Path(__file__).parent.parent.parent
    file_path = base_url / "user_memories"
    return ChromadbClient(file_path)


# 模块级单例实例
chromadb_client = get_chromadb_client()
