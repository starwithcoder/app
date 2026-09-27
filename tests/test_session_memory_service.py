"""会话记忆服务集成测试（连真实 Redis，不调真实 LLM）。

由原 `python -m services.session_memory_service` 自测块迁移而来，覆盖：
消息记录、短期记忆上下文装配、摘要注入、墓碑删除、摘要 JSON 解析、中期召回。
"""
from typing import Tuple

from services.session_memory_service import SessionMemoryService, session_memory_service as svc


async def test_record_message_assigns_increasing_ids(session_ids: Tuple[str, str]) -> None:
    user_id, session_id = session_ids
    id1 = await svc.record_message(user_id, session_id, "user", "我喜欢用 Postgres")
    id2 = await svc.record_message(user_id, session_id, "assistant", "记下了，你偏好 Postgres")
    assert id1 == 1
    assert id2 == 2


async def test_prepare_context_structure(session_ids: Tuple[str, str]) -> None:
    """上下文结构：[system] + [摘要] + [近期消息] + [本次输入]。"""
    user_id, session_id = session_ids
    await svc.record_message(user_id, session_id, "user", "我喜欢用 Postgres")
    await svc.record_message(user_id, session_id, "assistant", "记下了，你偏好 Postgres")

    ctx = await svc.prepare_context(user_id, session_id, "那 MySQL 呢？")

    assert ctx[0]["role"] == "system"
    assert ctx[-1] == {"role": "user", "content": "那 MySQL 呢？"}
    history = [m for m in ctx if m["role"] in ("user", "assistant")]
    assert len(history) == 3  # 2 条历史 + 1 条本次输入


async def test_prepare_context_includes_summary(session_ids: Tuple[str, str]) -> None:
    """滚动摘要属于短期记忆上下文的一部分（长期记忆压缩后的产物）。"""
    user_id, session_id = session_ids
    await svc._repo.set_summary(user_id, session_id, "用户偏好 Postgres")

    ctx = await svc.prepare_context(user_id, session_id, "再问一次")
    assert any("用户偏好 Postgres" in m["content"] for m in ctx)


async def test_tombstone_delete_filters_message(session_ids: Tuple[str, str]) -> None:
    """墓碑删除：被删消息不进上下文，但会留一条墓碑消息让模型知晓。"""
    user_id, session_id = session_ids
    id1 = await svc.record_message(user_id, session_id, "user", "我喜欢用 Postgres")
    await svc.record_delete(user_id, session_id, [id1])

    ctx = await svc.prepare_context(user_id, session_id, "测试删除")
    assert not any(m["content"] == "我喜欢用 Postgres" for m in ctx)
    assert any("已删除消息" in m["content"] for m in ctx)


async def test_parse_summary_json_with_code_fence() -> None:
    """模型常把 JSON 包在 ```json 围栏里，必须能解析。"""
    parsed = SessionMemoryService._parse_summary_json('```json\n{"summary": "x", "facts": []}\n```')
    assert parsed is not None
    assert parsed["summary"] == "x"


async def test_parse_summary_json_invalid_returns_none() -> None:
    assert SessionMemoryService._parse_summary_json("not json") is None


async def test_parse_summary_json_missing_summary_returns_none() -> None:
    """缺 summary 字段视为失败 —— 调用方会记失败计数并下轮重试。"""
    assert SessionMemoryService._parse_summary_json('{"facts": []}') is None


async def test_recall_recent_includes_current_session(session_ids: Tuple[str, str]) -> None:
    """中期记忆：召回该用户最近几个会话的摘要。

    注意：会话是在写入消息（append_message）时才注册进活跃集合的（懒创建），
    所以这里必须先记一条消息，否则 recall_recent 扫不到该会话。
    """
    user_id, session_id = session_ids
    await svc.record_message(user_id, session_id, "user", "你好")
    await svc._repo.set_summary(user_id, session_id, "用户偏好 Postgres")

    recent = await svc.recall_recent(user_id, window=3)
    assert any(r["session_id"] == session_id for r in recent)
