"""Redis 会话仓储集成测试（连真实 Redis）。

由原 `python -m repositories.redis_session_repository` 自测块迁移而来，覆盖：
pending 追加/读取、summary 读写、摘要水位线、墓碑删除、per-session 锁、
活跃会话 zset、pending 裁剪、会话整体清理。

每个用例通过 `session_ids` fixture 拿到干净的会话，结束自动清理，互不干扰。
"""
from typing import Tuple

from repositories.redis_session_repository import redis_session_repository as repo


async def test_append_message_assigns_increasing_ids(session_ids: Tuple[str, str]) -> None:
    """append_message 分配单调递增 id —— 墓碑删除依赖它定位消息。"""
    user_id, session_id = session_ids
    id1 = await repo.append_message(user_id, session_id, {"role": "user", "message": "你好"})
    id2 = await repo.append_message(
        user_id, session_id, {"role": "assistant", "message": "你好，有什么可以帮你"}
    )
    assert id1 == 1
    assert id2 == 2
    assert await repo.pending_len(user_id, session_id) == 2


async def test_get_pending_returns_all_messages(session_ids: Tuple[str, str]) -> None:
    user_id, session_id = session_ids
    await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})
    await repo.append_message(user_id, session_id, {"role": "assistant", "message": "b"})
    assert len(await repo.get_pending(user_id, session_id)) == 2


async def test_get_recent_pending(session_ids: Tuple[str, str]) -> None:
    user_id, session_id = session_ids
    await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})
    id2 = await repo.append_message(user_id, session_id, {"role": "assistant", "message": "b"})

    recent = await repo.get_recent_pending(user_id, session_id, 1)
    assert len(recent) == 1
    assert recent[0]["id"] == id2  # 取最近的一条

    assert await repo.get_recent_pending(user_id, session_id, 0) == []


async def test_summary_roundtrip(session_ids: Tuple[str, str]) -> None:
    user_id, session_id = session_ids
    await repo.set_summary(user_id, session_id, "摘要：打招呼")
    assert await repo.get_summary(user_id, session_id) == "摘要：打招呼"


async def test_summary_watermark(session_ids: Tuple[str, str]) -> None:
    """水位线：记录"已摘要到哪"，是增量摘要不重复劳动的依据。"""
    user_id, session_id = session_ids
    await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})
    await repo.append_message(user_id, session_id, {"role": "assistant", "message": "b"})

    assert await repo.pending_since_summary(user_id, session_id) == 2

    await repo.mark_summarized(user_id, session_id)
    assert await repo.pending_since_summary(user_id, session_id) == 0


async def test_tombstone_delete(session_ids: Tuple[str, str]) -> None:
    """墓碑删除：不真删数据，只记 id，构建上下文时过滤掉。"""
    user_id, session_id = session_ids
    id1 = await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})
    id2 = await repo.append_message(user_id, session_id, {"role": "assistant", "message": "b"})

    await repo.mark_deleted(user_id, session_id, [id1])
    deleted = await repo.get_deleted_ids(user_id, session_id)
    assert deleted == {str(id1)}

    filtered = repo.filter_deleted(await repo.get_pending(user_id, session_id), deleted)
    assert [m["id"] for m in filtered] == [id2]


async def test_lock_is_mutually_exclusive(session_ids: Tuple[str, str]) -> None:
    """per-session 锁：同一会话同时只能有一个实例在归档（跨进程也生效）。"""
    user_id, session_id = session_ids
    token = await repo.acquire_lock(user_id, session_id, ttl=10)
    assert token is not None

    # 未释放前再抢一定失败
    assert await repo.acquire_lock(user_id, session_id, ttl=10) is None

    assert await repo.release_lock(user_id, session_id, token) is True


async def test_lock_can_be_reacquired_after_release(session_ids: Tuple[str, str]) -> None:
    user_id, session_id = session_ids
    first = await repo.acquire_lock(user_id, session_id, ttl=10)
    assert first is not None
    await repo.release_lock(user_id, session_id, first)

    second = await repo.acquire_lock(user_id, session_id, ttl=10)
    assert second is not None
    await repo.release_lock(user_id, session_id, second)


async def test_active_sessions_zset(session_ids: Tuple[str, str]) -> None:
    """活跃会话集合：扫描协程靠它枚举"有哪些会话要处理"。"""
    user_id, session_id = session_ids
    await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})

    member = f"{user_id}:{session_id}"
    assert member in await repo.get_active_sessions()
    assert await repo.get_last_active(user_id, session_id) is not None
    # idle 秒数取 0：刚活跃过的会话一定命中
    assert member in await repo.get_expired_sessions(0)


async def test_trim_pending(session_ids: Tuple[str, str]) -> None:
    user_id, session_id = session_ids
    await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})
    await repo.append_message(user_id, session_id, {"role": "assistant", "message": "b"})

    await repo.trim_pending(user_id, session_id, keep=1)
    assert await repo.pending_len(user_id, session_id) == 1

    await repo.trim_pending(user_id, session_id, keep=0)
    assert await repo.pending_len(user_id, session_id) == 0


async def test_delete_session_clears_everything(session_ids: Tuple[str, str]) -> None:
    """删除会话要清干净：pending / summary / meta / 墓碑 / 活跃集合，一处都不能漏。"""
    user_id, session_id = session_ids
    id1 = await repo.append_message(user_id, session_id, {"role": "user", "message": "a"})
    await repo.set_summary(user_id, session_id, "摘要")
    await repo.mark_deleted(user_id, session_id, [id1])

    assert await repo.delete_session(user_id, session_id) is True

    assert await repo.pending_len(user_id, session_id) == 0
    assert await repo.get_summary(user_id, session_id) is None
    assert await repo.get_deleted_ids(user_id, session_id) == set()
    assert f"{user_id}:{session_id}" not in await repo.get_active_sessions()
