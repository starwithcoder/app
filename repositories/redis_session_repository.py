"""会话短期记忆仓储（Redis 实现）。

职责边界：**只做数据访问**，不含业务逻辑与调度（业务在 services，调度在 tasks）。

管理的数据：
- pending  : 未归档消息列表（短期记忆，永远注入上下文）
- summary  : 滚动摘要文本
- active   : 活跃会话有序集合（score = 最后一条消息时间）
- deleted  : 墓碑集合（已删除消息的 id）
- meta     : 会话元数据（消息序号 / 已摘要水位线 / 上次摘要时间）
- lock     : per-session 归档锁（防止并发重复归档）

设计要点：
- 全部 async，底层用 redis.asyncio，不阻塞事件循环
- 每条消息都要调 touch_session 刷新活跃时间（否则活跃会话会被误判空闲而提前归档）
- 消息写入时分配单调递增 id，供墓碑删除引用
- 失败只记日志并返回安全默认值，不向外抛异常

键格式沿用既有约定：chat:sess:{user_id}:{session_id}:{suffix}
"""
import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from redis.exceptions import RedisError

from config.settings import settings
from infrastructure.database.redis_client import async_redis_client
from infrastructure.logging.logger import logger

# 活跃会话有序集合（member = "{user_id}:{session_id}"，score = 最后活跃时间戳）
ACTIVE_SESSIONS_KEY = "chat:active_sessions"

# 待删除会话队列（SET，member = "{user_id}:{session_id}"）
# 用户点删除时先进这里，由后台协程慢慢做「归档 → 导出 → 真删」，
# 放进 Redis 是为了多 worker 可见 + 重启不丢 + 只被消费一次
DELETION_QUEUE_KEY = "chat:deletion_queue"

# 释放锁的 Lua 脚本：只有 token 匹配才删除，避免误删别人的锁
_RELEASE_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


def _to_int(value: Any) -> int:
    """把 redis 返回的 hash 字段安全转成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    """把 redis 返回的 hash 字段安全转成 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class RedisSessionRepository:
    """会话短期记忆仓储（Redis）。"""

    def __init__(self, client=None):
        self._client = client or async_redis_client.client

    # ==================== 键名 ====================

    @staticmethod
    def pending_key(user_id: str, session_id: str) -> str:
        return f"chat:sess:{user_id}:{session_id}:pending"

    @staticmethod
    def summary_key(user_id: str, session_id: str) -> str:
        return f"chat:sess:{user_id}:{session_id}:summary"

    @staticmethod
    def deleted_key(user_id: str, session_id: str) -> str:
        return f"chat:sess:{user_id}:{session_id}:deleted"

    @staticmethod
    def meta_key(user_id: str, session_id: str) -> str:
        return f"chat:sess:{user_id}:{session_id}:meta"

    @staticmethod
    def lock_key(user_id: str, session_id: str) -> str:
        return f"chat:sess:{user_id}:{session_id}:lock"

    @staticmethod
    def _member(user_id: str, session_id: str) -> str:
        return f"{user_id}:{session_id}"

    @staticmethod
    def user_sessions_key(user_id: str) -> str:
        """用户的**持久会话索引**（会话目录，只增不删）。

        与 ACTIVE_SESSIONS_KEY 的区别（别混用）：
        - chat:active_sessions       = 扫描协程的"待办清单"，空闲久了会被移除
        - chat:user:{u}:sessions     = "会话目录"，侧边栏用它列出全部会话
        """
        return f"chat:user:{user_id}:sessions"

    # ==================== 连接 ====================

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except RedisError as e:
            logger.error(f"Redis ping 失败: {e}")
            return False

    # ==================== pending（短期记忆）====================

    async def append_message(
        self, user_id: str, session_id: str, message: Dict[str, Any]
    ) -> Optional[int]:
        """追加一条消息到 pending，并刷新活跃时间。

        会为消息分配单调递增 id 写入 message["id"]，供后续墓碑删除引用。

        Returns:
            Optional[int]: 分配的消息 id；失败返回 None。
        """
        try:
            msg_id = int(
                await self._client.hincrby(self.meta_key(user_id, session_id), "msg_seq", 1)
            )
            payload = dict(message)
            payload["id"] = msg_id
            await self._client.rpush(
                self.pending_key(user_id, session_id),
                json.dumps(payload, ensure_ascii=False),
            )
            # 注意：这里**不做**会话登记，会话只由 create_session 创建
            # （否则就出现第二个"创建点"，前端点一次新建会出现两条）
            # 刷新活跃时间：不刷新的话，正在聊的会话会被误判为空闲而提前归档
            await self.touch_session(user_id, session_id)
            return msg_id
        except (RedisError, TypeError, ValueError) as e:
            logger.exception(f"append_message 失败 user={user_id} session={session_id}: {e}")
            return None

    async def get_pending(self, user_id: str, session_id: str) -> List[Dict[str, Any]]:
        """取全部 pending 消息（时间正序）。"""
        return await self._range(user_id, session_id, 0, -1)

    async def get_recent_pending(
        self, user_id: str, session_id: str, k: int
    ) -> List[Dict[str, Any]]:
        """取最近 k 条 pending 消息（k<=0 返回空）。"""
        if k <= 0:
            return []
        return await self._range(user_id, session_id, -k, -1)

    async def pending_len(self, user_id: str, session_id: str) -> int:
        try:
            return int(await self._client.llen(self.pending_key(user_id, session_id)))
        except (RedisError, TypeError, ValueError) as e:
            logger.error(f"pending_len 失败: {e}")
            return 0

    async def clear_pending(self, user_id: str, session_id: str) -> bool:
        """清空 pending（LTRIM 1 0 会清空整个列表）。"""
        try:
            await self._client.ltrim(self.pending_key(user_id, session_id), 1, 0)
            return True
        except RedisError as e:
            logger.exception(f"clear_pending 失败: {e}")
            return False

    async def trim_pending(self, user_id: str, session_id: str, keep: int) -> bool:
        """裁剪 pending，只保留最近 keep 条（keep<=0 等价于清空）。"""
        if keep <= 0:
            return await self.clear_pending(user_id, session_id)
        try:
            await self._client.ltrim(self.pending_key(user_id, session_id), -keep, -1)
            return True
        except RedisError as e:
            logger.exception(f"trim_pending 失败: {e}")
            return False

    async def _range(
        self, user_id: str, session_id: str, start: int, end: int
    ) -> List[Dict[str, Any]]:
        try:
            raw = await self._client.lrange(self.pending_key(user_id, session_id), start, end)
        except RedisError as e:
            logger.error(f"lrange 失败: {e}")
            return []

        messages: List[Dict[str, Any]] = []
        for item in raw or []:
            try:
                messages.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"pending 消息解析失败，已跳过: {item!r}")
        return messages

    # ==================== summary（滚动摘要）====================

    async def set_summary(
        self, user_id: str, session_id: str, summary: str, ttl: Optional[int] = None
    ) -> bool:
        """写入滚动摘要。

        ttl 默认取 settings.REDIS_SUMMARY_TTL（7 天）。
        注意：不要用 REDIS_SESSION_TTL（那是短期记忆的，只有 1 天），
        摘要属于长期记忆，过期会导致用户隔几天回来记忆断掉。
        """
        try:
            await self._client.set(
                self.summary_key(user_id, session_id),
                summary,
                ex=ttl if ttl is not None else settings.REDIS_SUMMARY_TTL,
            )
            return True
        except RedisError as e:
            logger.exception(f"set_summary 失败: {e}")
            return False

    async def get_summary(self, user_id: str, session_id: str) -> Optional[str]:
        try:
            value = await self._client.get(self.summary_key(user_id, session_id))
            return str(value) if value is not None else None
        except RedisError as e:
            logger.error(f"get_summary 失败: {e}")
            return None

    # ==================== 活跃会话（zset）====================

    async def touch_session(self, user_id: str, session_id: str) -> bool:
        """刷新会话的最后活跃时间（每条消息都要调用）。"""
        try:
            await self._client.zadd(
                ACTIVE_SESSIONS_KEY,
                {self._member(user_id, session_id): time.time()},
            )
            return True
        except RedisError as e:
            logger.error(f"touch_session 失败: {e}")
            return False

    async def get_active_sessions(self) -> List[str]:
        """取全部活跃会话成员（"{user_id}:{session_id}"）。"""
        try:
            return [str(m) for m in await self._client.zrange(ACTIVE_SESSIONS_KEY, 0, -1)]
        except RedisError as e:
            logger.error(f"get_active_sessions 失败: {e}")
            return []

    async def get_expired_sessions(self, idle_seconds: float) -> List[str]:
        """取空闲超过 idle_seconds 的会话成员。"""
        try:
            return [
                str(m)
                for m in await self._client.zrangebyscore(
                    ACTIVE_SESSIONS_KEY, 0, time.time() - idle_seconds
                )
            ]
        except RedisError as e:
            logger.error(f"get_expired_sessions 失败: {e}")
            return []

    async def get_last_active(self, user_id: str, session_id: str) -> Optional[float]:
        try:
            score = await self._client.zscore(
                ACTIVE_SESSIONS_KEY, self._member(user_id, session_id)
            )
            return float(score) if score is not None else None
        except (RedisError, TypeError, ValueError) as e:
            logger.error(f"get_last_active 失败: {e}")
            return None

    async def remove_active_session(self, user_id: str, session_id: str) -> bool:
        try:
            await self._client.zrem(ACTIVE_SESSIONS_KEY, self._member(user_id, session_id))
            return True
        except RedisError as e:
            logger.error(f"remove_active_session 失败: {e}")
            return False

    # ==================== 会话索引 / 命名 ====================

    async def create_session(
        self, user_id: str, session_id: Optional[str] = None
    ) -> Optional[str]:
        """创建并登记一个新会话——**会话的唯一创建入口**。

        其它地方（发消息、存档等）都不应再登记会话，避免出现"第二个创建点"。

        幂等性：
        - 传入已存在的 session_id → 直接返回，不重复创建
        - 重复调用不传 id → 每次都生成新 id（所以前端只应调一次）

        Args:
            session_id: 不传则由后端生成，格式沿用前端约定
                        session_{毫秒时间戳}_{8位随机}

        Returns:
            Optional[str]: session_id；失败返回 None。
        """
        if session_id:
            # 已存在就复用，保证"只创建一次"
            try:
                if await self._client.sismember(self.user_sessions_key(user_id), session_id):
                    return session_id
            except RedisError as e:
                logger.error(f"create_session 检查会话是否存在失败: {e}")
            sid = session_id
        else:
            sid = f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

        try:
            await self._client.sadd(self.user_sessions_key(user_id), sid)
            # created_at 只写一次，重复调用（幂等）不会覆盖已有值
            await self._client.hsetnx(self.meta_key(user_id, sid), "created_at", time.time())
            return sid
        except RedisError as e:
            logger.exception(f"create_session 失败 user={user_id} session={sid}: {e}")
            return None

    async def list_user_sessions(self, user_id: str) -> List[str]:
        """取该用户全部会话 id（持久索引，不会因空闲被清理）。"""
        try:
            return [
                str(sid)
                for sid in await self._client.smembers(self.user_sessions_key(user_id))
            ]
        except RedisError as e:
            logger.error(f"list_user_sessions 失败: {e}")
            return []

    async def set_title(self, user_id: str, session_id: str, title: str) -> bool:
        """设置会话名（覆盖式）。"""
        try:
            await self._client.hset(
                self.meta_key(user_id, session_id), mapping={"title": title}
            )
            return True
        except RedisError as e:
            logger.exception(f"set_title 失败: {e}")
            return False

    async def remove_from_session_index(self, user_id: str, session_id: str) -> bool:
        """只从会话索引移除（列表里立刻消失），**不清任何数据**。

        用于软删除：用户点删除后前端立刻看不到，真实数据留给后台协程慢慢处理。
        """
        try:
            await self._client.srem(self.user_sessions_key(user_id), session_id)
            return True
        except RedisError as e:
            logger.exception(f"remove_from_session_index 失败: {e}")
            return False

    async def enqueue_deletion(self, user_id: str, session_id: str) -> bool:
        """把会话放进待删除队列（后台协程会来消费）。"""
        try:
            await self._client.sadd(DELETION_QUEUE_KEY, self._member(user_id, session_id))
            return True
        except RedisError as e:
            logger.exception(f"enqueue_deletion 失败: {e}")
            return False

    async def list_deletion_queue(self) -> List[str]:
        """取待删除队列的全部成员。"""
        try:
            return [str(m) for m in await self._client.smembers(DELETION_QUEUE_KEY)]
        except RedisError as e:
            logger.error(f"list_deletion_queue 失败: {e}")
            return []

    async def remove_from_deletion_queue(self, user_id: str, session_id: str) -> bool:
        """处理完成后从队列移除。"""
        try:
            await self._client.srem(DELETION_QUEUE_KEY, self._member(user_id, session_id))
            return True
        except RedisError as e:
            logger.exception(f"remove_from_deletion_queue 失败: {e}")
            return False

    async def set_title_if_absent(
        self, user_id: str, session_id: str, title: str
    ) -> bool:
        """仅在会话名还为空时设置（首问自动命名用：只认第一条 user 消息）。"""
        try:
            return bool(
                await self._client.hsetnx(self.meta_key(user_id, session_id), "title", title)
            )
        except RedisError as e:
            logger.exception(f"set_title_if_absent 失败: {e}")
            return False

    # ==================== meta / 摘要水位线 ====================

    _META_DEFAULTS: Dict[str, Any] = {
        "msg_seq": 0,
        "flushed_seq": 0,
        "summarized_seq": 0,
        "last_summary_ts": 0.0,
        "summary_fail_count": 0,
        "title": "",
        "created_at": 0.0,
    }

    async def get_meta(self, user_id: str, session_id: str) -> Dict[str, Any]:
        """取会话元数据。

        字段：
        - msg_seq            : 累计写入的消息数（单调递增）
        - flushed_seq        : 已落库到 Chroma 的水位线
        - summarized_seq     : 已摘要的水位线
        - last_summary_ts    : 上次摘要时间
        - summary_fail_count : 摘要连续失败次数（超过阈值转死信，不再重试）
        - title              : 会话名（首问自动生成，侧边栏展示用）
        - created_at         : 会话创建时间戳
        """
        try:
            raw = await self._client.hgetall(self.meta_key(user_id, session_id))
        except RedisError as e:
            logger.error(f"get_meta 失败: {e}")
            return dict(self._META_DEFAULTS)

        meta = dict(self._META_DEFAULTS)
        for key in ("msg_seq", "flushed_seq", "summarized_seq", "summary_fail_count"):
            meta[key] = _to_int(raw.get(key))
        meta["last_summary_ts"] = _to_float(raw.get("last_summary_ts"))
        meta["created_at"] = _to_float(raw.get("created_at"))
        meta["title"] = str(raw.get("title") or "")
        return meta

    async def mark_summarized(self, user_id: str, session_id: str) -> bool:
        """把"已摘要水位线"推进到当前最新消息，并记录摘要时间。"""
        try:
            meta_key = self.meta_key(user_id, session_id)
            msg_seq = await self._client.hget(meta_key, "msg_seq") or 0
            await self._client.hset(
                meta_key,
                mapping={"summarized_seq": int(msg_seq), "last_summary_ts": time.time()},
            )
            return True
        except (RedisError, TypeError, ValueError) as e:
            logger.exception(f"mark_summarized 失败: {e}")
            return False

    async def pending_since_summary(self, user_id: str, session_id: str) -> int:
        """距上次摘要新增了多少条消息（用于判断是否触发摘要）。"""
        meta = await self.get_meta(user_id, session_id)
        return max(0, meta["msg_seq"] - meta["summarized_seq"])

    async def mark_flushed(self, user_id: str, session_id: str, seq: int) -> bool:
        """推进"已落库"水位线（避免下一轮重复写入 Chroma）。"""
        try:
            await self._client.hset(
                self.meta_key(user_id, session_id), mapping={"flushed_seq": int(seq)}
            )
            return True
        except (RedisError, TypeError, ValueError) as e:
            logger.exception(f"mark_flushed 失败: {e}")
            return False

    async def pending_since_flush(self, user_id: str, session_id: str) -> int:
        """距上次落库新增了多少条消息。"""
        meta = await self.get_meta(user_id, session_id)
        return max(0, meta["msg_seq"] - meta["flushed_seq"])

    async def bump_summary_failure(self, user_id: str, session_id: str) -> int:
        """摘要失败计数 +1，返回当前累计值。"""
        try:
            return int(
                await self._client.hincrby(
                    self.meta_key(user_id, session_id), "summary_fail_count", 1
                )
            )
        except (RedisError, TypeError, ValueError) as e:
            logger.exception(f"bump_summary_failure 失败: {e}")
            return 0

    async def reset_summary_failure(self, user_id: str, session_id: str) -> bool:
        """摘要成功后清零失败计数。"""
        try:
            await self._client.hset(
                self.meta_key(user_id, session_id), mapping={"summary_fail_count": 0}
            )
            return True
        except RedisError as e:
            logger.exception(f"reset_summary_failure 失败: {e}")
            return False

    # ==================== 墓碑删除 ====================

    async def mark_deleted(
        self, user_id: str, session_id: str, msg_ids: Iterable[Any]
    ) -> bool:
        """把消息 id 标记为已删除（墓碑）。"""
        ids = [str(i) for i in msg_ids]
        if not ids:
            return True
        try:
            await self._client.sadd(self.deleted_key(user_id, session_id), *ids)
            return True
        except RedisError as e:
            logger.exception(f"mark_deleted 失败: {e}")
            return False

    async def get_deleted_ids(self, user_id: str, session_id: str) -> Set[str]:
        try:
            return {str(m) for m in await self._client.smembers(self.deleted_key(user_id, session_id))}
        except RedisError as e:
            logger.error(f"get_deleted_ids 失败: {e}")
            return set()

    @staticmethod
    def filter_deleted(
        messages: List[Dict[str, Any]], deleted_ids: Iterable[Any]
    ) -> List[Dict[str, Any]]:
        """过滤掉已被标记删除的消息（按 message["id"] 匹配）。"""
        deleted = {str(i) for i in deleted_ids or ()}
        if not deleted:
            return messages
        return [m for m in messages if str(m.get("id")) not in deleted]

    # ==================== per-session 锁 ====================

    async def acquire_lock(self, user_id: str, session_id: str, ttl: int = 30) -> Optional[str]:
        """获取 per-session 归档锁（跨进程生效）。

        Returns:
            Optional[str]: 成功返回 token（释放时需要），已被占用或失败返回 None。
        """
        token = uuid.uuid4().hex
        try:
            ok = await self._client.set(
                self.lock_key(user_id, session_id), token, nx=True, ex=ttl
            )
            return token if ok else None
        except RedisError as e:
            logger.error(f"acquire_lock 失败: {e}")
            return None

    async def release_lock(self, user_id: str, session_id: str, token: str) -> bool:
        """释放锁（Lua 校验 token 一致才删除）。"""
        try:
            await self._client.eval(
                _RELEASE_LOCK_LUA, 1, self.lock_key(user_id, session_id), token
            )
            return True
        except RedisError as e:
            logger.error(f"release_lock 失败: {e}")
            return False

    # ==================== 会话清理 ====================

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除会话：清短期数据 + 移出活跃集合 + 从持久索引移除。"""
        try:
            await self._client.delete(
                self.pending_key(user_id, session_id),
                self.summary_key(user_id, session_id),
                self.deleted_key(user_id, session_id),
                self.meta_key(user_id, session_id),
            )
            await self._client.srem(self.user_sessions_key(user_id), session_id)
            await self.remove_active_session(user_id, session_id)
            return True
        except RedisError as e:
            logger.exception(f"delete_session 失败: {e}")
            return False


# 全局单例
redis_session_repository = RedisSessionRepository()


# 测试已迁移到 tests/test_redis_session_repository.py：pytest tests/ -v


#测试