"""会话记忆后台扫描协程（归档 + 摘要）。

定位：这是**后台任务**，只负责调度与编排——
数据访问在 repositories，上下文装配在 services，LLM 调用由外部注入。

每个活跃会话每一轮扫描做什么：
1. 拿 per-session 锁（拿不到说明别的实例在归档，直接跳过）
2. 摘要：距上次摘要新增 ≥K 条 或 距上次 ≥T 秒 → 一次 LLM 调用产出 {summary, facts[]}
3. 落库：把 flushed_seq 之后的新消息写入 Chroma，推进水位线（避免重复写）
4. 裁剪：pending 保留"未摘要的 + 最近窗口"，防止无限增长
5. 清理：空闲超过 CLEANUP_IDLE 的会话 → 强制归档后移出活跃集合（防止 pending 泄漏）

关键设计：
- **触发不判断"用户是否活跃"**：活跃与否不影响归档与摘要（旧版把摘要绑在 idle 上是错的）
- **顺序必须是 摘要 → 落库 → 裁剪**：裁剪会丢消息，所以要在摘要之后
- **KEEP_RECENT 必须 ≥ SUMMARY_MIN_NEW**：未摘要的永远是最新的消息，
  保证窗口足够大，未摘要消息就不会被裁剪掉
- **失败不推进水位线**：落库/摘要失败时水位线不动，下轮自动重试；
  摘要连续失败超过上限转死信，避免无限烧 LLM
"""
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.settings import settings
from infrastructure.logging.logger import logger
from repositories.memory_repository import session_collection as chroma_session_collection
from repositories.memory_repository import summary_collection as chroma_summary_collection
from repositories.redis_session_repository import redis_session_repository
from services.session_memory_service import session_memory_service

# ==================== 阈值配置 ====================

SCAN_INTERVAL = 60.0            # 默认轮询间隔（优先用 settings.SCAN_INTERVAL）
SUMMARY_MIN_NEW = 20            # K：距上次摘要新增 ≥K 条触发摘要
SUMMARY_MAX_INTERVAL = 30 * 60  # T：距上次摘要 ≥T 秒触发摘要
CLEANUP_IDLE = 7 * 24 * 3600    # 空闲超过 7 天：最终归档后移出活跃集合
KEEP_RECENT = 50                # pending 保留最近多少条（短期记忆窗口）
LOCK_TTL = 60                   # per-session 锁 TTL（秒）
MAX_SUMMARY_ATTEMPTS = 3        # 摘要连续失败上限，超过转死信不再重试

if KEEP_RECENT < SUMMARY_MIN_NEW:
    raise ValueError("KEEP_RECENT 必须 >= SUMMARY_MIN_NEW，否则未摘要的消息会被裁剪丢失")


def _msg_id(message: Dict[str, Any]) -> int:
    """取消息 id（append_message 分配的单调递增序号）。"""
    try:
        return int(message.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _msg_content(message: Dict[str, Any]) -> str:
    """取消息正文，兼容 "message" / "content" 两种字段名。"""
    return str(message.get("message") or message.get("content") or "")


class SessionScanner:
    """会话归档 / 摘要扫描器（常驻协程）。"""

    def __init__(
        self,
        repo=None,
        summarize_fn: Optional[Callable[..., Any]] = None,
        session_collection=None,
        summary_collection=None,
        facts_collection=None,
        scan_interval: Optional[float] = None,
    ):
        self.repo = repo or redis_session_repository
        # 摘要 + 事实抽取的实现在 services 层，scanner 只负责调度
        self._summarize_fn = (
            summarize_fn
            if summarize_fn is not None
            else session_memory_service.summarize_and_extract
        )
        self._session_collection = (
            session_collection if session_collection is not None else chroma_session_collection
        )
        self._summary_collection = (
            summary_collection if summary_collection is not None else chroma_summary_collection
        )
        self._facts_collection = facts_collection
        self.scan_interval = float(scan_interval or settings.SCAN_INTERVAL or SCAN_INTERVAL)
        self._stopping = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    # ==================== 常驻循环 ====================

    async def run_forever(self) -> None:
        """常驻循环（由 start() 包装成 Task）。"""
        logger.info(f"[scanner] 启动，轮询间隔 {self.scan_interval}s")
        while not self._stopping.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[scanner] 本轮扫描异常")
            await asyncio.sleep(self.scan_interval)
        logger.info("[scanner] 已停止")

    def start(self) -> asyncio.Task[None]:
        """启动常驻协程（在 FastAPI lifespan 启动时调用）。"""
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self.run_forever())
        return self._task

    async def stop(self) -> None:
        """停止常驻协程（在 FastAPI lifespan 关闭时调用）。"""
        self._stopping.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ==================== 单轮扫描 ====================

    async def scan_once(self) -> None:
        """扫描一轮：遍历所有活跃会话。"""
        members = await self.repo.get_active_sessions()
        if not members:
            return
        logger.debug(f"[scanner] 本轮活跃会话 {len(members)} 个")
        for member in members:
            if ":" not in member:
                continue
            user_id, session_id = member.split(":", 1)
            try:
                await self._process_session(user_id, session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[scanner] 处理会话失败 member={member}")

    # ==================== 单会话处理 ====================

    async def _process_session(self, user_id: str, session_id: str) -> None:
        """处理单个会话：拿锁 → 摘要 → 落库 → 裁剪 → 空闲清理。"""
        if self._stopping.is_set():
            return

        token = await self.repo.acquire_lock(user_id, session_id, ttl=LOCK_TTL)
        if not token:
            return  # 别的实例 / 上一轮还在归档，跳过

        try:
            pending = await self.repo.get_pending(user_id, session_id)
            if not pending:
                await self._maybe_cleanup(user_id, session_id, pending=[])
                return

            # 顺序关键：先摘要（此时 pending 里还有全部未摘要消息）
            await self._maybe_summarize(
                user_id, session_id, pending, await self.repo.get_meta(user_id, session_id)
            )
            # 再落库
            await self._flush(
                user_id, session_id, pending, await self.repo.get_meta(user_id, session_id)
            )
            # 最后裁剪（此时该摘要的摘要过、该落库的落库了）
            await self._trim(user_id, session_id)

            await self._maybe_cleanup(user_id, session_id, pending=pending)
        finally:
            await self.repo.release_lock(user_id, session_id, token)

    async def _maybe_summarize(
        self,
        user_id: str,
        session_id: str,
        pending: List[Dict[str, Any]],
        meta: Dict[str, Any],
        force: bool = False,
    ) -> None:
        """满足条件时做一次摘要 + 事实抽取。force=True 用于长期空闲前的最终归档。"""
        new_since = meta["msg_seq"] - meta["summarized_seq"]
        last_ts = meta["last_summary_ts"]
        elapsed = (time.time() - last_ts) if last_ts else None

        if not force:
            need = new_since >= SUMMARY_MIN_NEW or (
                elapsed is not None and elapsed >= SUMMARY_MAX_INTERVAL
            )
            if not need:
                return
            if meta["summary_fail_count"] >= MAX_SUMMARY_ATTEMPTS:
                logger.error(
                    f"[scanner] 摘要连续失败 {meta['summary_fail_count']} 次"
                    f"（上限 {MAX_SUMMARY_ATTEMPTS}），转死信不再重试 "
                    f"user={user_id} session={session_id}"
                )
                return

        to_summarize = [m for m in pending if _msg_id(m) > meta["summarized_seq"]]
        if not to_summarize:
            return

        old_summary = await self.repo.get_summary(user_id, session_id) or ""
        result = await self._summarize_fn(old_summary, to_summarize)

        if not result or not result.get("summary"):
            count = await self.repo.bump_summary_failure(user_id, session_id)
            logger.warning(f"[scanner] 摘要失败（第 {count} 次）user={user_id} session={session_id}")
            return

        # 成功：写摘要 → 写事实 → 推进水位线 → 清零失败计数
        await self.repo.set_summary(user_id, session_id, result["summary"])
        await self._store_summary_to_chroma(user_id, session_id, result["summary"])
        await self._store_facts(user_id, session_id, result.get("facts") or [])
        await self.repo.mark_summarized(user_id, session_id)
        await self.repo.reset_summary_failure(user_id, session_id)
        logger.info(
            f"[scanner] 已摘要 user={user_id} session={session_id} "
            f"新增 {len(to_summarize)} 条，事实 {len(result.get('facts') or [])} 条"
        )

    async def _flush(
        self,
        user_id: str,
        session_id: str,
        pending: List[Dict[str, Any]],
        meta: Dict[str, Any],
    ) -> None:
        """把 flushed_seq 之后的新消息写入 Chroma，成功后推进水位线。"""
        if self._session_collection is None:
            logger.error("[scanner] Chroma 会话集合不可用（连接失败？），跳过落库")
            return

        new_ones = [m for m in pending if _msg_id(m) > meta["flushed_seq"]]
        if not new_ones:
            return

        ok = True
        for msg in new_ones:
            content = _msg_content(msg)
            if not content:
                continue
            res = await self._session_collection.add_memory(
                user_id, session_id, {"role": msg.get("role", ""), "message": content}
            )
            ok = ok and bool(res)

        if ok:
            await self.repo.mark_flushed(user_id, session_id, max(_msg_id(m) for m in new_ones))
            logger.info(f"[scanner] 落库 {len(new_ones)} 条 user={user_id} session={session_id}")
        else:
            # 不推进水位线，下轮会重试这些消息（宁可重复写也不丢，但 add 是幂等的不会真重复）
            logger.warning(
                f"[scanner] 部分消息落库失败，flushed_seq 不推进，下轮重试 "
                f"user={user_id} session={session_id}"
            )

    async def _trim(self, user_id: str, session_id: str) -> None:
        """裁剪 pending，保留"未摘要的 + 最近窗口"。"""
        meta = await self.repo.get_meta(user_id, session_id)
        pending = await self.repo.get_pending(user_id, session_id)
        unsummarized = sum(1 for m in pending if _msg_id(m) > meta["summarized_seq"])
        keep = max(KEEP_RECENT, unsummarized)
        if len(pending) > keep:
            await self.repo.trim_pending(user_id, session_id, keep=keep)

    async def _store_summary_to_chroma(self, user_id: str, session_id: str, summary: str) -> None:
        """把摘要同步一份到 Chroma，便于后续语义检索历史摘要。"""
        if self._summary_collection is None:
            return
        try:
            await self._summary_collection.add_summary(user_id, session_id, summary)
        except Exception:
            logger.exception("[scanner] 摘要写入 Chroma 失败（Redis 已写入，不影响主流程）")

    async def _store_facts(
        self, user_id: str, session_id: str, facts: List[Dict[str, Any]]
    ) -> None:
        """写入原子事实库（FactsCollection 实现后接入）。"""
        if not facts:
            return
        if self._facts_collection is None:
            logger.warning("[scanner] facts_collection 未注入，跳过事实写入（FactsCollection 待实现）")
            return
        for fact in facts:
            try:
                await self._facts_collection.add_fact(user_id, session_id, fact)
            except Exception:
                logger.exception("[scanner] 写入原子事实失败")

    async def _maybe_cleanup(
        self, user_id: str, session_id: str, pending: List[Dict[str, Any]]
    ) -> None:
        """长期空闲清理：强制归档再移出活跃集合，避免 pending 永久泄漏。"""
        last_active = await self.repo.get_last_active(user_id, session_id)
        if last_active is None:
            return
        if time.time() - last_active < CLEANUP_IDLE:
            return

        if pending:
            meta = await self.repo.get_meta(user_id, session_id)
            # 强制摘要一次，保证走前留下记忆
            await self._maybe_summarize(user_id, session_id, pending, meta, force=True)
            await self._flush(
                user_id, session_id, pending, await self.repo.get_meta(user_id, session_id)
            )

        await self.repo.clear_pending(user_id, session_id)
        await self.repo.remove_active_session(user_id, session_id)
        logger.info(
            f"[scanner] 会话空闲超过 {CLEANUP_IDLE}s，已最终归档并移出活跃集合 "
            f"user={user_id} session={session_id}"
        )


# 全局单例
session_scanner = SessionScanner()


if __name__ == "__main__":
    # 自测：python -m tasks.session_scanner
    # 用 stub 摘要函数 + stub 集合，不产生真实 LLM 调用，也不往 Chroma 写脏数据

    class _StubSessionCollection:
        def __init__(self):
            self.written: List[Dict[str, Any]] = []

        async def add_memory(self, user_id: str, session_id: str, message: Dict[str, Any]) -> bool:
            self.written.append(message)
            return True

    class _StubSummaryCollection:
        def __init__(self):
            self.written: List[str] = []

        async def add_summary(self, user_id: str, session_id: str, summary: str) -> bool:
            self.written.append(summary)
            return True

    async def _stub_summarize(old_summary: str, messages: List[Dict[str, Any]]):
        return {
            "summary": f"[stub] 已摘要 {len(messages)} 条",
            "facts": [{"category": "task", "subject": "测试", "content": "这是一条测试事实"}],
        }

    async def _main() -> None:
        sess_col, summ_col = _StubSessionCollection(), _StubSummaryCollection()
        scanner = SessionScanner(
            summarize_fn=_stub_summarize,
            session_collection=sess_col,
            summary_collection=summ_col,
        )
        if not await scanner.repo.ping():
            print("Redis 不可用，跳过自测")
            return

        user_id, session_id = "__test_user__", "__test_session__"
        await scanner.repo.delete_session(user_id, session_id)

        # 降低阈值，让 3 条消息也能触发摘要
        # 注意：必须用 globals()，python -m 运行时本模块就是 __main__
        globals()["SUMMARY_MIN_NEW"] = 1

        for i in range(3):
            await scanner.repo.append_message(
                user_id, session_id, {"role": "user", "message": f"测试消息 {i}"}
            )

        results: List[Tuple[str, bool]] = []

        def check(name: str, ok: bool, detail: Any = "") -> None:
            results.append((name, bool(ok)))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

        before = await scanner.repo.get_meta(user_id, session_id)
        check("扫描前水位线为 0", before["flushed_seq"] == 0 and before["summarized_seq"] == 0, before)

        await scanner.scan_once()

        after = await scanner.repo.get_meta(user_id, session_id)
        check("落库 3 条到 Chroma(stub)", len(sess_col.written) == 3, f"{len(sess_col.written)} 条")
        check("flushed_seq 推进到 3", after["flushed_seq"] == 3, f"flushed_seq={after['flushed_seq']}")
        check("summarized_seq 推进到 3", after["summarized_seq"] == 3, f"summarized_seq={after['summarized_seq']}")
        check("last_summary_ts 已写入", after["last_summary_ts"] > 0)
        check("失败计数为 0", after["summary_fail_count"] == 0)
        check("摘要写入 Redis", (await scanner.repo.get_summary(user_id, session_id)) == "[stub] 已摘要 3 条")
        check("摘要镜像到 Chroma(stub)", len(summ_col.written) == 1)

        # 幂等：再扫一轮不应重复落库、也不应重复摘要
        await scanner.scan_once()
        check("重复扫描不重复落库", len(sess_col.written) == 3, f"{len(sess_col.written)} 条")
        check("重复扫描不重复摘要", len(summ_col.written) == 1)

        await scanner.repo.delete_session(user_id, session_id)
        check("清理测试数据", await scanner.repo.pending_len(user_id, session_id) == 0)

        failed = [n for n, ok in results if not ok]
        print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
        print("全部通过" if not failed else f"失败项: {failed}")

    asyncio.run(_main())
