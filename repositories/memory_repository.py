"""基于 ChromaDB 的记忆仓储（实体级数据访问）。

职责分层：
- infrastructure.database.chromadb_client.BaseCollection 提供"通用 CRUD"（技术实现）
- 本模块定义"具体实体集合"，遵循 repositories → infrastructure 的单向依赖

包含：
- SessionCollection : 会话原始消息（memories_collection）
- SummaryCollection : 会话摘要（summary_collection）
- FactsCollection   : 原子事实库（facts_collection）

FactsCollection 是**长期记忆**：跨会话（按 user_id 检索）、按 subject 去重更新。
"""
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from infrastructure.database.chromadb_client import BaseCollection, get_chromadb_client

# collection 名称：从配置读取
# 换 embedding 模型时改配置即可切到新集合（旧向量与新模型不兼容，不能混用）
MEMORIES_COLLECTION = settings.MEMORIES_COLLECTION
SUMMARY_COLLECTION = settings.SUMMARY_COLLECTION
FACTS_COLLECTION = settings.FACTS_COLLECTION


class SessionCollection(BaseCollection):
    """会话记忆集合（对应 memories_collection）。"""

    collection_name = MEMORIES_COLLECTION

    async def add_memory(self, user_id: str, session_id: str, message: Dict[str, Any]) -> bool:
        return await self.add(
            user_id=user_id,
            session_id=session_id,
            document=message.get("message"),
            role=message.get("role"),
        )


class SummaryCollection(BaseCollection):
    """会话摘要集合（对应 summary_collection）。"""

    collection_name = SUMMARY_COLLECTION

    async def add_summary(self, user_id: str, session_id: str, summary: str) -> bool:
        return await self.add(
            user_id=user_id,
            session_id=session_id,
            document=summary,
            role="summary",
        )


class FactsCollection(BaseCollection):
    """原子事实库（长期记忆）。

    与会话/摘要集合的两点关键差异：
    1. **跨会话**：按 user_id 检索，不绑定某个 session——
       用户在 A 会话说的偏好，B 会话也能查到。
    2. **按 subject 去重（upsert）**：写入时先删掉同 subject 的旧事实再写新的，
       天然处理"用户改主意了"（比如数据库从 MySQL 换成 Postgres），
       检索到的永远是最新的那条。
    """

    collection_name = FACTS_COLLECTION

    @staticmethod
    def _normalize_subject(subject: Any) -> str:
        """归一化 subject —— 它是"覆盖旧事实"的匹配键，必须稳定。

        为什么需要：subject 由 LLM 生成，同一主题可能写成
        「数据库选型」/「数据库 选型」/「Database选型」，字符串不相等
        就会导致旧事实删不掉、新旧两条并存。

        注意：这里只能解决**字面漂移**（空白、大小写）。
        「数据库选型」vs「数据库方案」这类同义不同词的写法，
        靠抽取 prompt 要求模型复用已有 subject 来解决（见
        services/session_memory_service.summarize_and_extract）。

        实测过"语义去重"方案（按向量距离覆盖）：在 bge-m3 下
        「MySQL→PostgreSQL」距离 0.55，而「张三前端→李四后端」距离 0.60，
        两者差距太小没有安全阈值，会误合并不同事实，因此不采用。
        """
        return "".join(str(subject or "").split()).lower()

    async def add_fact(self, user_id: str, session_id: str, fact: Dict[str, Any]) -> bool:
        """写入 / 更新一条原子事实。

        Args:
            fact: {"category": ..., "subject": ..., "content": ..., "entities": [...]}

        Returns:
            bool: 是否写入成功（content 为空时返回 False）。
        """
        content = str(fact.get("content") or "").strip()
        if not content:
            return False

        subject = self._normalize_subject(fact.get("subject"))
        category = str(fact.get("category") or "").strip()
        entities = fact.get("entities") or []
        # chromadb 的 metadata 不支持 list / dict，必须序列化成字符串
        entities_str = (
            ",".join(str(e) for e in entities)
            if isinstance(entities, (list, tuple, set))
            else str(entities)
        )

        # upsert：先清掉同 subject 的旧事实，再写入新的
        if subject:
            await self.delete_where(
                {"$and": [{"user_id": str(user_id)}, {"subject": subject}]}
            )

        return await self.add(
            user_id=user_id,
            session_id=session_id,
            document=content,
            role="fact",
            extra_metadata={
                "category": category,
                "subject": subject,
                "entities": entities_str,
            },
        )

    async def query_facts(
        self,
        user_id: str,
        question: str,
        category: Optional[str] = None,
        n_results: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """跨会话检索长期事实（可按 category 过滤）。"""
        return await self.query_by_user(
            user_id,
            question,
            n_results=n_results,
            extra_where={"category": category} if category else None,
        )

    # ---- 轻量混合检索（分类软加权 + 关键词融合）----
    # 设计目标：不引入额外 LLM 调用、不改变写入逻辑，只在「检索」这一步叠加两层
    # 廉价信号：
    #   1) 规则分类：从问题里猜一个 category，给同类事实加分（软加权，不硬过滤，
    #      避免分类错了把正确答案漏掉；hard negative 通常和正确答案同 category，
    #      所以真正的区分靠第 2 层）
    #   2) 关键词（字符 bigram 重叠）：纯字符串匹配，零成本，对「同领域但措辞不同」
    #      的 hard negative 区分度极高（如 8080 vs 8081、PostgreSQL vs MySQL）

    @staticmethod
    def _char_bigrams(text: str) -> set:
        text = "".join(ch for ch in str(text) if ch.isalnum())
        return {text[i : i + 2] for i in range(len(text) - 1)}

    @staticmethod
    def _lexical_score(question: str, doc: str) -> float:
        """字符 bigram 的 Jaccard 重叠度，衡量「问题」与「事实」的字面重合。"""
        qb, db = FactsCollection._char_bigrams(question), FactsCollection._char_bigrams(doc)
        union = qb | db
        return (len(qb & db) / len(union)) if union else 0.0

    @staticmethod
    def _classify_category(question: str) -> Optional[str]:
        """规则分类：从问题里猜一个 category（best-effort，猜错返回 None 不生效）。"""
        q = str(question)
        rules = {
            "people": ["谁", "负责人", "前端", "后端", "团队", "分工", "同事"],
            "decision": ["定了", "方案", "选", "拍板", "决定", "采用", "部署", "上线"],
            "preference": ["偏好", "喜欢", "习惯", "工具", "格式化", "中意", "倾向", "用"],
            "project": ["项目", "服务", "端口", "地址", "部署", "应用", "app", "库"],
            "task": ["几点", "时间", "备份", "任务", "每隔", "每天", "频率"],
        }
        for cat, kws in rules.items():
            if any(k in q for k in kws):
                return cat
        return None

    async def hybrid_query_facts(
        self,
        user_id: str,
        question: str,
        n_results: int = 5,
        lexical_weight: float = 0.5,
        category_weight: float = 0.1,
    ) -> Optional[Dict[str, Any]]:
        """轻量混合检索：向量排序 + 关键词融合 + 类别软加权。

        不改变 Chroma 返回结构（仍是 ids/documents/metadatas/distances 嵌套列表），
        只是把候选池（向量召回 top-N）按融合分重排，上层可直接复用。
        """
        # 先向量召回一个较大的候选池（不硬过滤 category，避免漏掉正确答案）
        pool = max(n_results * 2, 10)
        res = await self.query_by_user(user_id, question, n_results=pool)
        documents = (res or {}).get("documents") or []
        metadatas = (res or {}).get("metadatas") or []
        distances = (res or {}).get("distances") or []
        if not documents or not documents[0]:
            return res

        docs, metas, dists = documents[0], metadatas[0], distances[0]
        # 距离归一化为相似度（池内 min-max，避免依赖具体距离度量）
        dmin, dmax = min(dists), max(dists)
        cat = self._classify_category(question)

        scored = []
        for doc, meta, dist in zip(docs, metas, dists):
            vec_sim = (dmax - dist) / (dmax - dmin) if dmax > dmin else 1.0
            # entities 作为额外检索标签并入字面匹配（免费提升召回）
            entities = (meta or {}).get("entities") or ""
            lex_text = f"{doc} {entities}" if entities else doc
            lex = self._lexical_score(question, lex_text)
            cat_match = (
                category_weight if (cat and (meta or {}).get("category") == cat) else 0.0
            )
            fused = vec_sim * (1.0 - lexical_weight) + lex * lexical_weight + cat_match
            scored.append((fused, doc, meta, dist))

        scored.sort(key=lambda x: x[0], reverse=True)
        return {
            "ids": res.get("ids"),
            "documents": [[s[1] for s in scored]],
            "metadatas": [[s[2] for s in scored]],
            "distances": [[s[3] for s in scored]],
        }


# ---- 全局实例（连接失败时为 None，调用方需判空）----
_client = get_chromadb_client().client
session_collection = SessionCollection(_client) if _client else None
summary_collection = SummaryCollection(_client) if _client else None
facts_collection = FactsCollection(_client) if _client else None


if __name__ == "__main__":
    # 自测：python -m repositories.memory_repository
    import asyncio

    async def _main() -> None:
        if facts_collection is None:
            print("Chroma 不可用，跳过自测")
            return

        user_id = "__fact_test_user__"
        sess_a, sess_b = "sess_A", "sess_B"
        await facts_collection.delete_where({"user_id": user_id})

        results: List[Tuple[str, bool]] = []

        def check(name: str, ok: bool, detail: Any = "") -> None:
            results.append((name, bool(ok)))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

        def flat(resp: Optional[Dict[str, Any]]) -> List[str]:
            return [d for group in (resp or {}).get("documents") or [] for d in group]

        # 1. 会话 A 写入一条事实
        check(
            "add_fact 写入（会话A）",
            await facts_collection.add_fact(
                user_id, sess_a,
                {"category": "decision", "subject": "数据库选型",
                 "content": "项目使用 MySQL", "entities": ["MySQL"]},
            ),
        )

        # 2. 会话 B 更新同一 subject（用户改主意 → upsert 覆盖旧事实）
        check(
            "add_fact 更新同 subject（会话B）",
            await facts_collection.add_fact(
                user_id, sess_b,
                {"category": "decision", "subject": "数据库选型",
                 "content": "项目改用 Postgres", "entities": ["Postgres"]},
            ),
        )

        # 3. 另一条不同 subject
        await facts_collection.add_fact(
            user_id, sess_a,
            {"category": "preference", "subject": "编程语言",
             "content": "用户偏好 Python", "entities": ["Python"]},
        )

        # 4. 跨会话检索：会话B写入的 Postgres 要能被查到
        hits = flat(await facts_collection.query_facts(user_id, "数据库用什么", n_results=5))
        check("跨会话检索命中", any("Postgres" in d for d in hits), f"{hits}")
        check("旧事实已被覆盖（不含 MySQL）", not any("MySQL" in d for d in hits))

        # 5. 按 category 过滤
        pref = flat(
            await facts_collection.query_facts(
                user_id, "编程语言", category="preference", n_results=5
            )
        )
        check("category 过滤生效", bool(pref) and all("Python" in d for d in pref), f"{pref}")

        # 6. 清理
        await facts_collection.delete_where({"user_id": user_id})
        check("清理干净", len(flat(await facts_collection.query_facts(user_id, "数据库"))) == 0)

        failed = [n for n, ok in results if not ok]
        print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
        print("全部通过" if not failed else f"失败项: {failed}")

    asyncio.run(_main())
