"""pytest 共享 fixture。

为什么事件循环是 session 级：
`async_redis_client` 是全局单例，redis.asyncio 的连接在**首次使用时**绑定到当时
运行的事件循环上。若每个用例各用一个事件循环，复用同一连接会报
"Event loop is closed" / "attached to a different loop"。
所以 pytest.ini 里把 fixture 与 test 的 loop scope 都设成 session。

这些用例是**集成测试**：连真实 Redis。Redis 不可用时整体 skip，而不是判失败。
"""
from typing import AsyncIterator, Tuple

import pytest

from repositories.redis_session_repository import redis_session_repository as repo


@pytest.fixture(scope="session", autouse=True)
async def require_redis() -> None:
    """Redis 不可用时跳过全部集成测试（而不是判定为失败）。"""
    if not await repo.ping():
        pytest.skip("Redis 不可用，跳过需要 Redis 的集成测试")


@pytest.fixture
async def session_ids() -> AsyncIterator[Tuple[str, str]]:
    """一对隔离的 (user_id, session_id)：用例开始前清空，结束后自动清理。"""
    user_id, session_id = "__pytest_user__", "__pytest_sess__"
    await repo.delete_session(user_id, session_id)
    try:
        yield user_id, session_id
    finally:
        await repo.delete_session(user_id, session_id)
