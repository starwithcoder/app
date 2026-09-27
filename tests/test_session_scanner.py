"""扫描协程集成测试（连真实 Redis，但不碰真实 LLM / 真实 Chroma）。

设计要点：
- 摘要函数用 stub → **不产生真实 LLM 调用**
- Chroma 三个集合（session / summary / facts）全部用 stub → **不写脏数据**
- 只调 `_process_session`（单会话），**不用 `scan_once`**
  —— 后者会遍历所有活跃会话，会连带处理到真实用户的数据
"""
from typing import Any, AsyncIterator, Dict, List, Tuple

import pytest

import tasks.session_scanner as scanner_module
from repositories.redis_session_repository import redis_session_repository as repo
from tasks.session_scanner import SessionScanner


class StubSessionCollection:
    def __init__(self) -> None:
        self.written: List[Dict[str, Any]] = []

    async def add_memory(self, user_id: str, session_id: str, message: Dict[str, Any]) -> bool:
        self.written.append(message)
        return True


class StubSummaryCollection:
    def __init__(self) -> None:
        self.written: List[str] = []

    async def add_summary(self, user_id: str, session_id: str, summary: str) -> bool:
        self.written.append(summary)
        return True


class StubFactsCollection:
    def __init__(self) -> None:
        self.written: List[Dict[str, Any]] = []

    async def add_fact(self, user_id: str, session_id: str, fact: Dict[str, Any]) -> bool:
        self.written.append(fact)
        return True


async def _stub_summarize(old_summary: str, messages: List[Dict[str, Any]], user_id: str = ""):
    return {
        "summary": f"[stub] 已摘要 {len(messages)} 条",
        "facts": [{"category": "task", "subject": "测试", "content": "这是一条测试事实"}],
    }


@pytest.fixture
async def scanner_env(
    session_ids: Tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Tuple[SessionScanner, StubSessionCollection, StubSummaryCollection, StubFactsCollection, str, str]]:
    """准备一个"3 条待处理消息 + stub 依赖"的扫描环境。"""
    # 降低阈值，让 3 条消息也能触发摘要
    monkeypatch.setattr(scanner_module, "SUMMARY_MIN_NEW", 1)

    user_id, session_id = session_ids
    sess_col, summ_col, facts_col = (
        StubSessionCollection(),
        StubSummaryCollection(),
        StubFactsCollection(),
    )
    for i in range(3):
        await repo.append_message(
            user_id, session_id, {"role": "user", "message": f"测试消息 {i}"}
        )

    scanner = SessionScanner(
        repo=repo,
        summarize_fn=_stub_summarize,
        session_collection=sess_col,
        summary_collection=summ_col,
        facts_collection=facts_col,
    )
    yield scanner, sess_col, summ_col, facts_col, user_id, session_id


async def test_first_pass_flushes_summarizes_and_stores_facts(scanner_env) -> None:
    scanner, sess_col, summ_col, facts_col, user_id, session_id = scanner_env

    before = await repo.get_meta(user_id, session_id)
    assert before["flushed_seq"] == 0
    assert before["summarized_seq"] == 0

    await scanner._process_session(user_id, session_id)

    after = await repo.get_meta(user_id, session_id)
    # 落库：3 条消息写入 Chroma，水位线推进
    assert len(sess_col.written) == 3
    assert after["flushed_seq"] == 3
    # 摘要：一次 LLM 调用产出 summary + facts
    assert after["summarized_seq"] == 3
    assert after["last_summary_ts"] > 0
    assert after["summary_fail_count"] == 0
    assert await repo.get_summary(user_id, session_id) == "[stub] 已摘要 3 条"
    assert len(summ_col.written) == 1
    assert len(facts_col.written) == 1


async def test_second_pass_is_idempotent(scanner_env) -> None:
    """水位线的核心价值：重复处理不会重复落库、也不会重复摘要。"""
    scanner, sess_col, summ_col, facts_col, user_id, session_id = scanner_env

    await scanner._process_session(user_id, session_id)
    await scanner._process_session(user_id, session_id)

    assert len(sess_col.written) == 3
    assert len(summ_col.written) == 1
    assert len(facts_col.written) == 1


async def test_flush_without_summary_when_below_threshold(
    scanner_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """触发条件是"内容/时间"，不是"活跃"：没到阈值就只落库、不摘要（省 LLM 成本）。"""
    monkeypatch.setattr(scanner_module, "SUMMARY_MIN_NEW", 10)

    scanner, sess_col, summ_col, facts_col, user_id, session_id = scanner_env
    await scanner._process_session(user_id, session_id)

    after = await repo.get_meta(user_id, session_id)
    assert len(sess_col.written) == 3  # 仍然落库
    assert after["flushed_seq"] == 3
    assert after["summarized_seq"] == 0  # 没触发摘要
    assert summ_col.written == []
    assert facts_col.written == []
