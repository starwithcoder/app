"""会话记忆服务（业务编排层）。

分层边界：
- repositories : 只做数据访问（Redis 短期 / Chroma 长期）
- services     : 业务编排（本模块）——上下文装配、消息记录、摘要与事实抽取、召回
- tasks        : 后台调度（session_scanner，只调本模块，不自己写业务逻辑）

对外能力：
- prepare_context       : 装配短期记忆上下文（LLM / Agent 的输入）
- record_message / record_turn / record_delete : 消息记录与墓碑删除
- summarize_and_extract : 一次 LLM 调用产出 {summary, facts[]}
- recall_* / get_summary: 中长期召回（后续挂成 Agent 工具）

约定：
- 消息字段统一用 OpenAI 风格 {"role": ..., "content": ...}
  （Chroma 侧用 "message"，读写时由 repositories 层兼容）
- prepare_context 只读不写；写入必须显式调 record_*
"""
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config.settings import settings
from infrastructure.logging.logger import logger
from repositories.memory_repository import session_collection as chroma_session_collection
from repositories.memory_repository import summary_collection as chroma_summary_collection
from repositories.redis_session_repository import RedisSessionRepository, redis_session_repository

# 短期记忆：注入上下文的最近消息条数
DEFAULT_RECENT_K = 20


class SessionMemoryService:
    """会话记忆服务（业务编排）。"""

    def __init__(
        self,
        repo=None,
        session_collection=None,
        summary_collection=None,
        facts_collection=None,
    ):
        self._repo = repo or redis_session_repository
        self._sessions = (
            session_collection if session_collection is not None else chroma_session_collection
        )
        self._summaries = (
            summary_collection if summary_collection is not None else chroma_summary_collection
        )
        self._facts = facts_collection

    # ==================== 短期记忆：上下文装配 ====================

    @staticmethod
    def _content_of(message: Dict[str, Any]) -> str:
        """取消息正文，兼容 "content" / "message" 两种字段。"""
        return str(message.get("content") or message.get("message") or "")

    @staticmethod
    def _default_system_prompt(session_id: str) -> str:
        return f"你是一个有记忆的智能体助手，请基于上下文历史回答用户问题（会话ID {session_id}）。"

    async def prepare_context(
        self,
        user_id: str,
        session_id: str,
        user_input: str,
        system_prompt: Optional[str] = None,
        recent_k: int = DEFAULT_RECENT_K,
    ) -> List[Dict[str, str]]:
        """装配短期记忆上下文：[system] + [滚动摘要] + [pending 最近 K 条] + [本次输入]。

        注意：本方法**只读**，不会写入任何消息。
        写入请显式调用 record_message / record_turn。

        Returns:
            List[Dict[str, str]]: OpenAI 风格消息列表（role + content）。
        """
        summary = await self._repo.get_summary(user_id, session_id)
        pending = await self._repo.get_recent_pending(user_id, session_id, recent_k)
        # 墓碑过滤：被删除的消息不进上下文
        deleted = await self._repo.get_deleted_ids(user_id, session_id)
        pending = RedisSessionRepository.filter_deleted(pending, deleted)

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt or self._default_system_prompt(session_id)}
        ]

        if summary:
            messages.append(
                {"role": "system", "content": f"以下是此前对话的滚动摘要：\n{summary}"}
            )

        for msg in pending:
            content = self._content_of(msg)
            if content:
                messages.append({"role": msg.get("role", "user"), "content": content})

        messages.append({"role": "user", "content": user_input})
        return messages

    # ==================== 消息记录 / 删除 ====================

    async def record_message(
        self, user_id: str, session_id: str, role: str, content: str
    ) -> Optional[int]:
        """记录一条消息到短期记忆（Redis pending），返回消息 id。"""
        if not content:
            
            return None
        return await self._repo.append_message(
            user_id, session_id, {"role": role, "content": content}
        )

    async def record_turn(
        self, user_id: str, session_id: str, user_input: str, assistant_reply: str
    ) -> None:
        """记录一整轮对话（user + assistant）。"""
        await self.record_message(user_id, session_id, "user", user_input)
        await self.record_message(user_id, session_id, "assistant", assistant_reply)

    async def record_delete(
        self, user_id: str, session_id: str, msg_ids: Iterable[Any]
    ) -> bool:
        """墓碑删除：标记 deleted_ids，并追加一条墓碑消息让模型知晓。

        注意：按设计，**已生成的摘要不会因删除而回溯**（摘要是历史的压缩印象）。
        """
        ids = [str(i) for i in msg_ids or ()]
        if not ids:
            return True
        ok = await self._repo.mark_deleted(user_id, session_id, ids)
        await self.record_message(
            user_id, session_id, "system", f"（用户已删除消息：{', '.join(ids)}）"
        )
        return ok

    # ==================== 摘要 + 事实抽取（一次 LLM 调用）====================

    async def summarize_and_extract(
        self, old_summary: str, messages: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """一次 LLM 调用同时产出滚动摘要与原子事实。

        这是 tasks/session_scanner 里 summarize_fn 的真实实现
        （scanner 只负责调度，不自己写业务逻辑）。

        Returns:
            Optional[Dict]: {"summary": str, "facts": [...]}；失败返回 None（调用方会计数重试）。
        """
        if not settings.SUB_MODEL_NAME:
            logger.warning("未配置 SUB_MODEL_NAME，跳过摘要与事实抽取")
            return None

        # 懒加载：避免没配 LLM 时 import 阶段就失败
        from infrastructure.ai.openai_client import sub_model_client

        dialog = "\n".join(f"{m.get('role', 'user')}: {self._content_of(m)}" for m in messages)
        prompt = (
            "你是记忆整理助手。请根据【已有摘要】和【新增对话】完成两件事：\n"
            "1. 更新滚动摘要：合并新旧信息，不超过 500 字，保留关键决策、偏好、项目进展。\n"
            "2. 抽取原子事实：从新增对话中抽出可长期复用的知识点，每条一个事实。\n\n"
            "严格只输出 JSON，不要任何解释文字，格式如下：\n"
            '{"summary": "...", "facts": [{"category": "decision|preference|project|people|task",'
            ' "subject": "事实主语", "content": "事实正文", "entities": ["实体"]}]}\n\n'
            f"【已有摘要】\n{old_summary or '（无）'}\n\n"
            f"【新增对话】\n{dialog}"
        )

        try:
            resp = await sub_model_client.chat.completions.create(
                model=settings.SUB_MODEL_NAME,
                messages=[
                    {"role": "system", "content": "你是一个严谨的记忆整理助手，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            return self._parse_summary_json(resp.choices[0].message.content or "")
        except Exception:
            logger.exception("摘要 + 事实抽取的 LLM 调用失败")
            return None

    @staticmethod
    def _parse_summary_json(content: str) -> Optional[Dict[str, Any]]:
        """解析模型输出，兼容 ```json 代码围栏与纯 JSON。"""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error(f"摘要 JSON 解析失败，原始输出: {str(content)[:200]!r}")
            return None

        if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
            logger.error(f"摘要 JSON 结构不符合预期: {str(data)[:200]}")
            return None

        facts = data.get("facts") or []
        if not isinstance(facts, list):
            facts = []
        return {"summary": data["summary"], "facts": facts}

    # ==================== 中长期召回（供 Agent 工具调用）====================

    async def get_summary(self, user_id: str, session_id: str) -> str:
        """长期记忆：取本会话滚动摘要。"""
        return (await self._repo.get_summary(user_id, session_id)) or ""

    async def recall_recent(self, user_id: str, window: int = 3) -> List[Dict[str, Any]]:
        """中期记忆：取该用户最近 window 个会话的摘要。"""
        members = await self._repo.get_active_sessions()
        prefix = f"{user_id}:"
        # get_active_sessions 按 score 升序返回，取最近的需要反转
        mine = [m for m in reversed(members) if m.startswith(prefix)]

        result: List[Dict[str, Any]] = []
        for member in mine[:window]:
            session_id = member.split(":", 1)[1]
            summary = await self._repo.get_summary(user_id, session_id)
            if summary:
                result.append({"session_id": session_id, "summary": summary})
        return result

    async def recall_search(
        self, user_id: str, session_id: str, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """中期记忆：在 Chroma 叙事库做语义检索（当前为会话内检索）。"""
        if self._sessions is None:
            logger.warning("Chroma 会话集合不可用，recall_search 返回空")
            return []

        results = await self._sessions.query(user_id, session_id, query, n_results=top_k)
        documents = (results or {}).get("documents") or []
        metadatas = (results or {}).get("metadatas") or []

        out: List[Dict[str, Any]] = []
        for docs, metas in zip(documents, metadatas):
            for doc, meta in zip(docs, metas):
                out.append(
                    {
                        "content": doc,
                        "role": (meta or {}).get("role"),
                        "timestamp": (meta or {}).get("timestamp"),
                    }
                )
        return out

    async def recall_facts(
        self, user_id: str, query: str, category: Optional[str] = None, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """长期记忆：检索原子事实库。

        FactsCollection 实现后生效，接口约定：query(user_id, query, category=None, n_results=5)。
        """
        if self._facts is None:
            logger.warning("facts_collection 未注入，recall_facts 暂不可用（FactsCollection 待实现）")
            return []
        results = await self._facts.query(user_id, query, category=category, n_results=top_k)
        return results or []


# 全局单例
session_memory_service = SessionMemoryService()


if __name__ == "__main__":
    # 自测：python -m services.session_memory_service
    import asyncio

    async def _main() -> None:
        svc = session_memory_service
        if not await svc._repo.ping():
            print("Redis 不可用，跳过自测")
            return

        user_id, session_id = "__test_user__", "__test_session__"
        await svc._repo.delete_session(user_id, session_id)

        results: List[Tuple[str, bool]] = []

        def check(name: str, ok: bool, detail: Any = "") -> None:
            results.append((name, bool(ok)))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

        # 1. 记录消息
        id1 = await svc.record_message(user_id, session_id, "user", "我喜欢用 Postgres")
        id2 = await svc.record_message(user_id, session_id, "assistant", "记下了，你偏好 Postgres")
        check("record_message 分配递增 id", id1 == 1 and id2 == 2, f"id={id1},{id2}")

        # 2. 装配上下文（短期记忆注入）
        ctx = await svc.prepare_context(user_id, session_id, "那 MySQL 呢？")
        check("首条是 system", ctx[0]["role"] == "system")
        check("末条是本次输入", ctx[-1] == {"role": "user", "content": "那 MySQL 呢？"})
        history = [m for m in ctx if m["role"] in ("user", "assistant")]
        check("含 2 条历史 + 1 条本次输入", len(history) == 3, f"{len(history)} 条")

        # 3. 滚动摘要进入上下文
        await svc._repo.set_summary(user_id, session_id, "用户偏好 Postgres")
        ctx2 = await svc.prepare_context(user_id, session_id, "再问一次")
        check(
            "摘要进入上下文",
            any("用户偏好 Postgres" in m["content"] for m in ctx2),
        )

        # 4. 墓碑删除
        await svc.record_delete(user_id, session_id, [id1])
        ctx3 = await svc.prepare_context(user_id, session_id, "测试删除")
        check(
            "被删消息不出现在上下文",
            not any(m["content"] == "我喜欢用 Postgres" for m in ctx3),
        )
        check("墓碑消息出现在上下文", any("已删除消息" in m["content"] for m in ctx3))

        # 5. 摘要 JSON 解析（不需要真实 LLM）
        parsed = SessionMemoryService._parse_summary_json(
            '```json\n{"summary": "x", "facts": []}\n```'
        )
        check("解析带代码围栏的 JSON", parsed is not None and parsed["summary"] == "x")
        check("解析非法 JSON 返回 None", SessionMemoryService._parse_summary_json("not json") is None)
        check(
            "解析缺 summary 字段返回 None",
            SessionMemoryService._parse_summary_json('{"facts": []}') is None,
        )

        # 6. 中期召回
        recent = await svc.recall_recent(user_id, window=3)
        check("recall_recent 取到本会话摘要", any(r["session_id"] == session_id for r in recent))

        await svc._repo.delete_session(user_id, session_id)
        check("清理测试数据", await svc._repo.pending_len(user_id, session_id) == 0)

        failed = [n for n, ok in results if not ok]
        print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
        print("全部通过" if not failed else f"失败项: {failed}")

    asyncio.run(_main())
