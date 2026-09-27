import asyncio
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.utils import embedding_functions

from config.settings import settings
from infrastructure.logging.logger import logger


class SiliconFlowEmbeddingFunction(embedding_functions.EmbeddingFunction):
    """调用硅基流动（OpenAI 兼容）的 `/embeddings` 接口做中文 embedding。

    为什么不用本地模型（如 BAAI/bge-small-zh-v1.5）：
    - 需要 sentence-transformers → 连带 torch（约 2GB）
    - 且要从 HuggingFace 下载模型（当前环境网络不可达）
    API 方案零本地依赖，直接复用已有的 SF_API_KEY。
    """

    def __init__(self, model_name: str, api_key: str, base_url: str, batch_size: int = 32):
        if not api_key:
            raise ValueError("embedding 缺少 API Key")
        if not base_url:
            raise ValueError("embedding 缺少 Base URL")
        self._model = model_name
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._batch_size = batch_size

    def __call__(self, input: List[str]) -> List[List[float]]:
        import httpx  # 局部导入，避免没用到时拖慢 import

        embeddings: List[List[float]] = []
        for start in range(0, len(input), self._batch_size):
            batch = input[start : start + self._batch_size]
            resp = httpx.post(
                self._url,
                headers=self._headers,
                json={"model": self._model, "input": batch, "encoding_format": "float"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            # 按 index 排序，保证返回顺序与输入一致
            embeddings.extend(
                item["embedding"]
                for item in sorted(data, key=lambda x: x.get("index", 0))
            )
        return embeddings


@lru_cache(maxsize=1)
def get_embedding_function():
    """获取 embedding 函数（带缓存，避免重复构造）。

    未配置 API Key / Base URL 时返回 None——此时 Chroma 会用自带的默认模型
    （英文 all-MiniLM），只能作为兜底，中文召回效果会很差。
    """
    api_key = settings.EMBEDDING_API_KEY or settings.SF_API_KEY
    base_url = settings.EMBEDDING_BASE_URL or settings.SF_BASE_URL
    if not api_key or not base_url:
        logger.error(
            "未配置 embedding 的 API Key / Base URL，回退到 Chroma 默认英文模型（中文召回效果差）"
        )
        return None

    return SiliconFlowEmbeddingFunction(
        model_name=settings.EMBEDDING_MODEL,
        api_key=api_key,
        base_url=base_url,
    )


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
            kwargs: Dict[str, Any] = {"name": self.collection_name}
            # 带上 embedding 函数；未配置时传 None 会让 Chroma 用默认（英文）模型
            ef = get_embedding_function()
            if ef is not None:
                kwargs["embedding_function"] = ef
            self._collection = self._client.get_or_create_collection(**kwargs)
        return self._collection

    async def add(
        self,
        user_id: str,
        session_id: str,
        document: Optional[str],
        role: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if document is None or document == "":
            logger.warning("add: 文档为空，已忽略")
            return False

        unique_id = f"{user_id}_{session_id}_{uuid.uuid4().hex}"
        timestamp = int(time.time())
        metadata: Dict[str, Any] = {
            "user_id": str(user_id),
            "session_id": str(session_id),
            "timestamp": timestamp,
            "role": str(role) if role is not None else "",
        }
        # 子类可附加业务元数据（注意：chromadb 只支持 str/int/float/bool，
        # list/dict 必须在子类里先序列化成字符串）
        if extra_metadata:
            metadata.update(extra_metadata)

        try:
            await asyncio.to_thread(
                self.collection.add,
                ids=[unique_id],
                documents=[document],
                metadatas=[metadata],
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

    async def all_by_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """取该用户在**所有会话**下的全部记录（跨会话）。

        与 all() 的区别：all() 还要限定 session_id，这个是只看 user_id。

        用途：
        - 长期记忆的"入库检查"（不检索，直接列出，确认某条事实到底存没存进去）
        - 前端侧边栏的会话列表聚合
        """
        try:
            results = await asyncio.to_thread(
                self.collection.get,
                where={"user_id": str(user_id)},
                include=["documents", "metadatas"],
            )
            return results
        except Exception:
            logger.exception(f"all_by_user: 获取集合 {self.collection_name} 中用户数据失败")
            return None

    async def query_by_user(
        self,
        user_id: str,
        question: str,
        n_results: int = 5,
        extra_where: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """按 user_id 语义检索（**跨会话**，长期记忆用）。

        与 query() 的区别：query() 限定在某个 session 内，
        query_by_user() 只看 user_id，因此能召回该用户在任何会话里存过的东西。

        Args:
            extra_where: 附加等值过滤条件（如 {"category": "decision"}），
                         值为 None 的项会被忽略。
        """
        try:
            filters: List[Dict[str, Any]] = [{"user_id": str(user_id)}]
            if extra_where:
                filters.extend(
                    {k: v} for k, v in extra_where.items() if v is not None
                )
            where = filters[0] if len(filters) == 1 else {"$and": filters}

            results = await asyncio.to_thread(
                self.collection.query,
                query_texts=[question],
                where=where,
                n_results=n_results,
            )
            return results
        except Exception:
            logger.exception(f"query_by_user: 查询集合 {self.collection_name} 失败")
            return None

    async def delete_where(self, where: Dict[str, Any]) -> bool:
        """按条件删除（用于 upsert 前清掉旧记录）。"""
        if not where:
            return True
        try:
            await asyncio.to_thread(self.collection.delete, where=where)
            return True
        except Exception:
            logger.exception(f"delete_where: 从集合 {self.collection_name} 删除失败")
            return False

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
