"""
Redis 客户端封装

职责：
1. 提供全局唯一的 Redis 连接（懒加载 + 单例）
2. 提供 ping 健康检查（用于启动自检）
3. 所有方法失败时只记日志、返回 None/False，不抛异常
   —— 保证 Redis 故障不影响主对话流程（降级到 JSON 文件存储）
"""
import redis
from redis.exceptions import RedisError

from config.settings import settings
from infrastructure.logging.logger import logger


class RedisClient:
    """Redis 客户端封装类（懒加载连接 + 故障降级）。"""

    def __init__(self):
        """初始化（不立即连接，首次使用时才建立连接）。"""
        self._client = None

    @property
    def client(self) -> redis.Redis:
        """获取 Redis 客户端实例（懒加载单例）。"""
        if self._client is None:
            self._client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,                    # 返回 str 而非 bytes
                socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                socket_timeout=settings.REDIS_CONNECT_TIMEOUT,
            )
        return self._client

    def ping(self) -> bool:
        """健康检查：能否连通 Redis。

        Returns:
            bool: True=连通，False=不可用（不抛异常）。
        """
        try:
            return bool(self.client.ping())
        except RedisError as e:
            logger.error(f"Redis 连接失败: {e}")
            return False

    def set(self, key: str, value: str, ttl: int = None) -> bool:
        """写入字符串值（可选过期时间）。"""
        try:
            self.client.set(key, value, ex=ttl or settings.REDIS_SESSION_TTL)
            return True
        except RedisError as e:
            logger.error(f"Redis 写入失败 key={key}: {e}")
            return False

    def get(self, key: str) -> str:
        """读取字符串值，失败/不存在时返回 None。"""
        try:
            return self.client.get(key)
        except RedisError as e:
            logger.error(f"Redis 读取失败 key={key}: {e}")
            return None

    def delete(self, key: str) -> bool:
        """删除 key。"""
        try:
            self.client.delete(key)
            return True
        except RedisError as e:
            logger.error(f"Redis 删除失败 key={key}: {e}")
            return False


# 全局单例
redis_client = RedisClient()


if __name__ == "__main__":
    # 连接自检：python redis_client.py
    ok = redis_client.ping()
    print(f"Redis 连接测试: {'成功' if ok else '失败'} ({settings.REDIS_URL})")
