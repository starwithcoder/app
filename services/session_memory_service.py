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
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config.settings import settings
from infrastructure.logging.logger import logger
from repositories.memory_repository import facts_collection as chroma_facts_collection
from repositories.memory_repository import session_collection as chroma_session_collection
from repositories.memory_repository import summary_collection as chroma_summary_collection
from repositories.redis_session_repository import RedisSessionRepository, redis_session_repository

# 短期记忆：注入上下文的最近消息条数（从配置读取）
DEFAULT_RECENT_K = settings.CONTEXT_MAX_RECENT


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
        self._facts = (
            facts_collection if facts_collection is not None else chroma_facts_collection
        )

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
        # 夹在配置的上下限之间：至少 CONTEXT_MIN_RECENT 条（保证连贯），
        # 最多 CONTEXT_MAX_RECENT 条（防止上下文过大）
        recent_k = max(
            settings.CONTEXT_MIN_RECENT, min(recent_k, settings.CONTEXT_MAX_RECENT)
        )

        summary = await self._repo.get_summary(user_id, session_id)
        pending = await self._repo.get_recent_pending(user_id, session_id, recent_k)
        # 墓碑过滤：被删除的消息不进上下文
        deleted = await self._repo.get_deleted_ids(user_id, session_id)
        pending = RedisSessionRepository.filter_deleted(pending, deleted)

        # 近期消息（只留有内容的）
        recent: List[Dict[str, str]] = []
        for msg in pending:
            content = self._content_of(msg)
            if content:
                recent.append({"role": msg.get("role", "user"), "content": content})

        system_msg = {
            "role": "system",
            "content": system_prompt or self._default_system_prompt(session_id),
        }
        summary_msg = (
            {"role": "system", "content": f"以下是此前对话的滚动摘要：\n{summary}"}
            if summary
            else None
        )
        user_msg: Dict[str, str] = {"role": "user", "content": user_input}

        # ---- token 预算裁剪 ----
        # 固定开销 = system + 摘要 + 本次输入；剩余预算用来装近期消息
        fixed_tokens = self._estimate_tokens(system_msg["content"]) + self._estimate_tokens(
            user_msg["content"]
        ) + (self._estimate_tokens(summary_msg["content"]) if summary_msg else 0)
        budget = settings.CONTEXT_MAX_TOKENS - fixed_tokens

        # 从**最新**往旧拿，保证最近的话一定在；
        # 但至少保留 CONTEXT_MIN_RECENT 条（下限优先于预算，保证上下文连贯）
        kept: List[Dict[str, str]] = []
        used = 0
        for msg in reversed(recent):
            cost = self._estimate_tokens(msg["content"])
            if used + cost > budget and len(kept) >= settings.CONTEXT_MIN_RECENT:
                break
            kept.insert(0, msg)
            used += cost

        messages: List[Dict[str, str]] = [system_msg]
        if summary_msg:
            messages.append(summary_msg)
        messages.extend(kept)
        messages.append(user_msg)
        return messages

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数（不引 tiktoken 的轻量方案）。

        中文约 1~1.5 token/字，英文约 0.25 token/词。
        这里统一按「字符数 × 1.5」估算，**偏保守**（会高估英文），宁可少装也别超模型窗口。
        若要更精确，可换成 tiktoken 按模型实际分词统计。
        """
        return int(len(text or "") * 1.5)

    # ==================== 消息记录 / 删除 ====================

    async def record_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        max_chars: Optional[int] = None,
    ) -> Optional[int]:
        """记录一条消息到短期记忆（Redis pending），返回消息 id。

        Args:
            max_chars: 入库前截断到多少字符，不传则不截断。
                       调用方按角色传：用户消息用 MAX_MESSAGE_CHARS，
                       助手回复用 MAX_REPLY_CHARS。
                       注意只截断"存档"，用户看到的流式输出不受影响。

        副作用：若是该会话的**第一条 user 消息**，会自动用它给会话命名。
        """
        if not content:

            return None
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "…"

        msg_id = await self._repo.append_message(
            user_id, session_id, {"role": role, "content": content}
        )
        # 首问自动命名（set_title_if_absent 保证只认第一条，后续消息不会覆盖）
        if msg_id and role == "user":
            await self._repo.set_title_if_absent(
                user_id, session_id, self._make_title(content)
            )
        return msg_id

    @staticmethod
    def _make_title(text: str, max_len: int = 20) -> str:
        """用首问生成会话名：压缩空白后截断。"""
        text = " ".join((text or "").split())
        return text if len(text) <= max_len else text[:max_len] + "…"

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

    async def get_session_messages(
        self, user_id: str, session_id: str, limit: int = settings.HISTORY_LOAD_LIMIT
    ) -> List[Dict[str, str]]:
        """取某个会话的消息记录（前端渲染聊天区用）。

        与 prepare_context 的区别：
        - prepare_context 是给 **LLM** 的输入（含 system / 摘要）
        - get_session_messages 是给 **前端** 展示的纯消息列表（role + content）

        当前只取 Redis pending 里保留的近期消息（短期记忆窗口，默认 50 条）。
        更早的消息已归档进 Chroma，若要做"完整历史"需再合并 Chroma。

        Returns:
            List[Dict[str, str]]: [{"role": ..., "content": ...}, ...]
        """
        pending = await self._repo.get_recent_pending(user_id, session_id, limit)
        deleted = await self._repo.get_deleted_ids(user_id, session_id)
        pending = RedisSessionRepository.filter_deleted(pending, deleted)

        messages: List[Dict[str, str]] = []
        for msg in pending:
            content = self._content_of(msg)
            if content:
                messages.append({"role": msg.get("role", "user"), "content": content})
        return messages

    # ==================== 会话管理（创建 / 列表）====================

    async def create_session(
        self, user_id: str, session_id: Optional[str] = None
    ) -> Optional[str]:
        """创建新会话（点击"新会话"时调用），返回 session_id。"""
        return await self._repo.create_session(user_id, session_id)

    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """列出该用户全部会话（侧边栏用）。

        只读持久索引 + 每个会话的 meta，**不加载消息正文**，
        比旧版"遍历 JSON 文件读全文"轻得多。

        Returns:
            List[Dict]: 按最近活跃时间倒序，每项含
                        session_id / title / create_time / total_messages / last_active
        """
        session_ids = await self._repo.list_user_sessions(user_id)

        items: List[Dict[str, Any]] = []
        for sid in session_ids:
            meta = await self._repo.get_meta(user_id, sid)
            last_active = await self._repo.get_last_active(user_id, sid)
            items.append(
                {
                    "session_id": sid,
                    "title": meta["title"] or "新对话",
                    "create_time": self._format_time(meta["created_at"]),
                    "last_active": self._format_time(last_active) if last_active else "",
                    "total_messages": meta["msg_seq"],
                    "_sort_ts": last_active or meta["created_at"] or 0.0,
                }
            )

        items.sort(key=lambda x: x["_sort_ts"], reverse=True)
        for item in items:
            item.pop("_sort_ts", None)
        return items

    @staticmethod
    def _format_time(ts: Optional[float]) -> str:
        """时间戳 → 'YYYY-MM-DD HH:MM:SS'。"""
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    # ==================== 摘要 + 事实抽取（一次 LLM 调用）====================

    async def summarize_and_extract(
        self,
        old_summary: str,
        messages: List[Dict[str, Any]],
        user_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """一次 LLM 调用同时产出滚动摘要与原子事实。

        这是 tasks/session_scanner 里 summarize_fn 的真实实现
        （scanner 只负责调度，不自己写业务逻辑）。

        Args:
            user_id: 传了才会把【已有事实主题】放进 prompt。
                没有它，模型在"用户改主意"时可能另起一个 subject，
                导致 add_fact 覆盖不掉旧事实、新旧两条并存。

        Returns:
            Optional[Dict]: {"summary": str, "facts": [...]}；失败返回 None（调用方会计数重试）。
        """
        if not settings.SUB_MODEL_NAME:
            logger.warning("未配置 SUB_MODEL_NAME，跳过摘要与事实抽取")
            return None

        # 懒加载：避免没配 LLM 时 import 阶段就失败
        from infrastructure.ai.openai_client import sub_model_client

        dialog = "\n".join(f"{m.get('role', 'user')}: {self._content_of(m)}" for m in messages)
        subject_hint = await self._build_subject_hint(user_id)
        prompt = (
            "你是记忆整理助手。请根据【已有摘要】和【新增对话】完成两件事：\n"
            f"1. 更新滚动摘要：合并新旧信息，不超过 {settings.SUMMARY_MAX_CHARS} 字，"
            "保留关键决策、偏好、项目进展。\n"
            "2. 抽取原子事实：从新增对话中抽出可长期复用的知识点，每条一个事实。\n\n"
            "抽取事实时必须遵守四条规则：\n"
            "【规则一】同一主题只保留最新结论。如果对话里出现反悔、更改、推翻"
            "（例如先说“用 MySQL”，后来说“换成 PostgreSQL”），"
            "**只输出更新后的那条**，不要同时保留旧值和新值。\n"
            "【规则二】subject 用简短的名词短语（如“数据库选型”“服务端口”），"
            "同一主题必须始终用完全相同的写法。\n"
            "【规则三】content 必须**自包含**：写成完整的一句话，并把 subject 的"
            "主题词写进句子里，让它脱离上下文也能看懂。\n"
            "  反例：“每天凌晨 3 点”（看不出是什么事的时间）\n"
            "  正例：“数据库备份时间是每天凌晨 3 点”\n"
            "  原因：检索只对 content 做向量匹配，subject 不参与，"
            "正文里缺主题词就检索不到。\n"
            "【规则四】一条事实只讲一件事，且只写最终结论。\n"
            "  不要把两个人塞进一条（“张三负责前端，李四负责后端”要拆成两条）。\n"
            "  鼓励用『决定 / 选择 / 采用 / 偏好』等词点明这是用户的**最终选择**而非随口一提"
            "——这能帮检索区分『用户选的』和『用户提过的』。\n"
            "  例如写成『用户决定使用 Redis 作为缓存中间件』，而不是笼统的『聊过缓存方案』。\n"
            "  但不要在 content 里展开被否决方案的细节（如『不考虑 Memcached』），"
            "那会稀释语义；被推翻的值靠 subject 去重自动覆盖即可。\n"
            "【规则五】entities 必须填写：本条事实涉及的**关键实体名 + 常见同义叫法**"
            "（工具名、人名、端口号、项目名、技术名词），只列当前有效的，不要列被推翻的旧值。\n"
            "  检索会把 entities 当作额外的『检索标签』，填得准能直接提升召回。\n"
            "  例：content 提了 ruff，entities 写 [\"ruff\", \"Python格式化工具\", \"代码格式化\"]。\n"
            f"{subject_hint}"
            "严格只输出 JSON，不要任何解释文字，格式如下：\n"
            '{"summary": "...", "facts": [{"category": "decision|preference|project|people|task",'
            ' "subject": "事实主语", "content": "完整的一句话，包含主题词，并点明用户的最终选择",'
            ' "entities": ["关键实体名", "同义叫法"]}]}\n\n'
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

    async def _build_subject_hint(self, user_id: Optional[str]) -> str:
        """拼出【已有事实主题】提示，让模型更新事实时复用同一个 subject。

        为什么需要：add_fact 靠 subject 精确匹配来覆盖旧事实。
        如果模型这次写“数据库选型”、下次写“数据库方案”，
        匹配不上就会新旧并存（实测中确实出现了这个问题）。
        把已有主题列表喂给模型，它就能原样复用。

        事实库不可用时返回空串（不影响主流程）。
        """
        if not user_id or self._facts is None:
            return ""
        try:
            known = await self._facts.all_by_user(user_id)
        except Exception:
            logger.exception("读取已有事实主题失败，跳过 subject 提示")
            return ""

        subjects: List[str] = []
        seen = set()
        for meta in (known or {}).get("metadatas") or []:
            s = str((meta or {}).get("subject") or "").strip()
            if s and s not in seen:
                seen.add(s)
                subjects.append(s)
        if not subjects:
            return ""

        # 只取最近 20 个，避免 prompt 过长
        return (
            "\n【已有事实主题】\n" + "、".join(subjects[:20]) + "\n"
            "若本次是对其中某个主题的**更新**，subject 必须原样复用，"
            "这样旧值才会被覆盖；不要另起新 subject。\n\n"
        )

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

    async def compress_summary(self, summary: str) -> str:
        """摘要超过 `SUMMARY_MAX_CHARS` 时触发压缩（"摘要的摘要"）。

        为什么需要：摘要是**增量合并**产生的（旧摘要 + 新消息 → 新摘要），
        所以会越滚越长。这一步是防止它无限膨胀的兜底。
        失败则返回原摘要（不阻塞主流程，下轮还会再试）。

        Args:
            summary: 当前摘要文本。

        Returns:
            str: 压缩后的摘要；未超限或失败时返回原文。
        """
        max_chars = settings.SUMMARY_MAX_CHARS
        if len(summary or "") <= max_chars:
            return summary

        if not settings.SUB_MODEL_NAME:
            logger.warning("未配置 SUB_MODEL_NAME，跳过摘要压缩")
            return summary

        # 懒加载：避免没配 LLM 时 import 阶段就失败
        from infrastructure.ai.openai_client import sub_model_client

        prompt = (
            f"下面是一段对话摘要，请把它压缩到 {max_chars} 字以内。\n"
            "要求：保留关键决策、用户偏好、项目进展；去掉细节与重复内容；\n"
            "只输出压缩后的摘要正文，不要任何解释文字。\n\n"
            f"【原摘要】\n{summary}"
        )
        try:
            resp = await sub_model_client.chat.completions.create(
                model=settings.SUB_MODEL_NAME,
                messages=[
                    {"role": "system", "content": "你是一个摘要压缩助手，只输出压缩结果。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            compressed = (resp.choices[0].message.content or "").strip()
            if not compressed:
                return summary
            logger.info(f"摘要压缩：{len(summary)} 字 → {len(compressed)} 字")
            return compressed
        except Exception:
            logger.exception("摘要压缩失败，保留原摘要")
            return summary

    # ==================== 删除会话（软删 + 后台真删）====================

    async def request_delete(self, user_id: str, session_id: str) -> bool:
        """用户点删除的**同步部分**，毫秒级返回。

        只做两件事：
        ① 从会话索引移除 → 前端列表立刻看不到（软删除，用户感知"秒删"）
        ② 加入删除队列 → 重活（归档 / 导出 / 真删）交给后台协程

        Returns:
            bool: 是否成功入队。
        """
        await self._repo.remove_from_session_index(user_id, session_id)
        ok = await self._repo.enqueue_deletion(user_id, session_id)
        if ok:
            logger.info(f"会话已标记删除并入队: user={user_id} session={session_id}")
        return ok

    async def process_one_deletion(self, user_id: str, session_id: str) -> bool:
        """处理一个待删除会话，**由后台协程**调用。

        顺序很关键：
        ① 先把 pending 里没归档的消息补进 Chroma  ← 不做这步直接清 Redis 会**丢数据**
        ② 导出完整数据到文件（分析用）
        ③ Chroma 真删（memories / summary / facts）
        ④ Redis 彻底清理
        ⑤ 从队列移除

        Returns:
            bool: 成功 True；失败则保留在队列，下轮自动重试（不会留下半删状态）。
        """
        try:
            # ① 归档：补写还没 flush 的消息，避免丢数据
            pending = await self._repo.get_pending(user_id, session_id)
            if pending and self._sessions is not None:
                for msg in pending:
                    content = self._content_of(msg)
                    if not content:
                        continue
                    await self._sessions.add_memory(
                        user_id,
                        session_id,
                        {"role": msg.get("role", ""), "message": content},
                    )

            # ② 导出（分析用）
            data = await self._collect_session_data(user_id, session_id)
            path = self._write_export(user_id, session_id, data)

            # ③ Chroma 真删
            where = {"$and": [{"user_id": str(user_id)}, {"session_id": str(session_id)}]}
            for col in (self._sessions, self._summaries, self._facts):
                if col is not None:
                    await col.delete_where(where)

            # ④ Redis 彻底清理
            await self._repo.delete_session(user_id, session_id)

            # ⑤ 出队
            await self._repo.remove_from_deletion_queue(user_id, session_id)
            logger.info(f"会话删除完成，已导出到 {path}: user={user_id} session={session_id}")
            return True
        except Exception:
            logger.exception(
                f"处理删除失败，保留在队列下轮重试: user={user_id} session={session_id}"
            )
            return False

    async def _collect_session_data(self, user_id: str, session_id: str) -> Dict[str, Any]:
        """收集一个会话的完整数据，供导出分析。"""
        data: Dict[str, Any] = {
            "user_id": user_id,
            "session_id": session_id,
            "deleted_at": time.time(),
        }

        # 短期：Redis pending 里的消息
        pending = await self._repo.get_pending(user_id, session_id)
        data["messages"] = [
            {"role": m.get("role", ""), "content": self._content_of(m)}
            for m in pending
            if self._content_of(m)
        ]

        # 长期：滚动摘要
        data["summary"] = await self._repo.get_summary(user_id, session_id) or ""

        # 中期：Chroma 里已归档的消息
        data["archived"] = []
        if self._sessions is not None:
            res = await self._sessions.all(user_id, session_id)
            docs = (res or {}).get("documents") or []
            metas = (res or {}).get("metadatas") or []
            for doc, meta in zip(docs, metas):
                data["archived"].append({"role": (meta or {}).get("role"), "content": doc})

        # 长期：原子事实
        data["facts"] = []
        if self._facts is not None:
            res = await self._facts.all(user_id, session_id)
            docs = (res or {}).get("documents") or []
            metas = (res or {}).get("metadatas") or []
            for doc, meta in zip(docs, metas):
                data["facts"].append(
                    {
                        "category": (meta or {}).get("category"),
                        "subject": (meta or {}).get("subject"),
                        "content": doc,
                    }
                )

        return data

    @staticmethod
    def _write_export(user_id: str, session_id: str, data: Dict[str, Any]) -> Path:
        """把导出数据写成 JSON 文件。"""
        export_dir = Path(settings.EXPORT_DIR)
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_sid = str(session_id).replace("/", "_").replace("\\", "_")
        path = export_dir / f"{user_id}__{safe_sid}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

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
        self,
        user_id: str,
        query: str,
        category: Optional[str] = None,
        top_k: int = 5,
        use_hybrid: bool = True,
    ) -> List[Dict[str, Any]]:
        """长期记忆：跨会话检索原子事实库，返回归一化后的事实列表。

        use_hybrid: 默认开启轻量混合检索（分类软加权 + 关键词融合），
                    关掉则退回到纯向量检索，用于 A/B 对比。
        """
        if self._facts is None:
            logger.warning("facts_collection 不可用（Chroma 连接失败？），recall_facts 返回空")
            return []

        if use_hybrid:
            results = await self._facts.hybrid_query_facts(user_id, query, n_results=top_k)
        else:
            results = await self._facts.query_facts(
                user_id, query, category=category, n_results=top_k
            )
        documents = (results or {}).get("documents") or []
        metadatas = (results or {}).get("metadatas") or []

        facts: List[Dict[str, Any]] = []
        for docs, metas in zip(documents, metadatas):
            for doc, meta in zip(docs, metas):
                meta = meta or {}
                facts.append(
                    {
                        "content": doc,
                        "category": meta.get("category", ""),
                        "subject": meta.get("subject", ""),
                        "entities": meta.get("entities", ""),
                        "timestamp": meta.get("timestamp"),
                    }
                )
        return facts


# 全局单例
session_memory_service = SessionMemoryService()


# 测试已迁移到 tests/test_session_memory_service.py：pytest tests/ -v
