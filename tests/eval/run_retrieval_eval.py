"""检索层（L1）评测：测"能不能把历史事实捞回来"，**全程不生成任何回答**。

两种模式
--------
full      完整链路：种子对话 → 强制归档 → LLM 抽取事实 → 检索
          测的是「抽取 + 检索」整体，包含 LLM 随机性

retrieval 跳过 LLM，直接把标准答案事实写进库，只测检索
          结果 100% 可复现，用来干净地评估 embedding / 检索质量

为什么必须先做"入库检查"
------------------------
没命中 ≠ 检索不准。可能是 LLM 压根没把这条事实抽出来（或抽错了）。
所以先列出库里所有事实，确认"到底存没存进去"，
把【抽取失败】和【检索失败】分开统计——
否则测出问题你根本不知道该改抽取 prompt 还是改 embedding。

指标
----
Recall@K  前 K 条里有没有命中（命中率）
MRR       命中位置的倒数（第 1 名=1.0，第 2 名=0.5 …），反映排序质量
违规      检索结果里出现了 must_not 的词（说明旧事实没被覆盖）

运行
----
python -m tests.eval.run_retrieval_eval                      # 默认 retrieval
python -m tests.eval.run_retrieval_eval --mode full
python -m tests.eval.run_retrieval_eval --top-k 10 --json out.json
"""
import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from infrastructure.database.chromadb_client import get_embedding_function
from repositories.memory_repository import (
    facts_collection,
    session_collection,
    summary_collection,
)
from repositories.redis_session_repository import redis_session_repository as repo
from services.session_memory_service import session_memory_service as svc
from tasks.session_scanner import SessionScanner

CASES_PATH = Path(__file__).parent / "cases.json"
SEED_SESSION = "eval_seed_session"


# ==================== 数据加载 ====================

def _load_cases() -> Tuple[List[str], List[Dict[str, Any]]]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data.get("noise_pool") or [], data.get("cases") or []


# ==================== 检索 / 入库检查 ====================

async def _list_facts(user_id: str) -> List[str]:
    """列出该用户**所有**已入库事实（不检索，用于入库检查）。"""
    if facts_collection is None:
        return []
    res = await facts_collection.all_by_user(user_id)
    return [d for d in ((res or {}).get("documents") or []) if d]


async def _query_facts(user_id: str, question: str, top_k: int, use_hybrid: bool = False) -> List[str]:
    """语义检索，返回按相似度排序的事实正文列表。

    use_hybrid: 走轻量混合检索（分类软加权 + 关键词融合）；否则纯向量。
    """
    if facts_collection is None:
        return []
    if use_hybrid:
        res = await facts_collection.hybrid_query_facts(user_id, question, n_results=top_k)
    else:
        res = await facts_collection.query_facts(user_id, question, n_results=top_k)
    documents = (res or {}).get("documents") or []
    if not documents:
        return []
    return [d for d in (documents[0] or []) if d]


def _rank_of(docs: List[str], expected: List[str]) -> Optional[int]:
    """返回第一个命中 expected 的位置（1 起）；没命中返回 None。"""
    for idx, doc in enumerate(docs):
        low = (doc or "").lower()
        for kw in expected:
            if kw.lower() in low:
                return idx + 1
    return None


def _violates(docs: List[str], must_not: List[str]) -> bool:
    """检索结果里是否出现了不该出现的旧事实。"""
    if not must_not:
        return False
    for doc in docs:
        low = (doc or "").lower()
        for kw in must_not:
            if kw.lower() in low:
                return True
    return False


# ==================== 归档 ====================

async def _force_archive(
    scanner: Optional[SessionScanner], user_id: str, session_id: str
) -> None:
    """强制走一遍归档（摘要 + 落库 + 事实抽取）。

    注意：直接调 _process_session 会因为"新增 < SUMMARY_MIN_NEW 条"而不摘要，
    种子对话只有 2~4 条时拿不到任何事实。这里用 force=True 绕过阈值，
    模拟"用户聊够了、系统真的归档了"之后的状态。
    """
    if scanner is None:
        return
    meta = await repo.get_meta(user_id, session_id)
    pending = await repo.get_pending(user_id, session_id)
    if not pending:
        return
    await scanner._maybe_summarize(user_id, session_id, pending, meta, force=True)
    await scanner._flush(user_id, session_id, pending, await repo.get_meta(user_id, session_id))


async def _cleanup(user_id: str) -> None:
    """清理评测数据：Chroma 三个集合 + Redis。"""
    if facts_collection is not None:
        await facts_collection.delete_where({"user_id": user_id})
    if session_collection:
        await session_collection.delete_where({"user_id": user_id})
    if summary_collection:
        await summary_collection.delete_where({"user_id": user_id})
    await repo.delete_session(user_id, SEED_SESSION)


# ==================== 单用例 ====================

async def run_case(
    case: Dict[str, Any],
    mode: str,
    top_k: int,
    noise_pool: List[str],
    scanner: Optional[SessionScanner],
    ts: int,
    use_hybrid: bool = False,
) -> Dict[str, Any]:
    # user_id 必须每用例独立：事实库是用户级的，共用会串味
    user_id = f"eval_{case['id']}_{mode}_{ts}"
    expected = case.get("expected") or []

    row: Dict[str, Any] = {
        "id": case["id"],
        "category": case.get("category", ""),
        "question": case.get("question", ""),
        "expected": expected,
        "in_store": False,
        "stored_count": 0,
        "rank": None,
        "top1": False,
        "mrr": 0.0,
        "violation": False,
        "distractor_count": len(case.get("distractors") or []),
    }

    try:
        if mode == "full":
            # ① 播种：种子对话写进 Redis pending（不调 LLM）
            for turn in case.get("seed") or []:
                await svc.record_message(
                    user_id, SEED_SESSION, turn["role"], turn["content"]
                )
            # ② 强制归档 → 摘要 + 落库 + 事实抽取
            await _force_archive(scanner, user_id, SEED_SESSION)
        else:
            if facts_collection is None:
                return row
            # 跳过 LLM，直接注入标准答案事实
            for fact in case.get("facts") or []:
                await facts_collection.add_fact(user_id, SEED_SESSION, fact)

        # ③ 注入干扰项（两种模式都做）
        #    它们代表"这个用户在别的会话里存过的事实"。
        #    放在归档**之后**注入，是为了不干扰 LLM 抽取——只用来考检索。
        if facts_collection is not None:
            # 简单噪声：跨领域，容易排除
            for i, text in enumerate(noise_pool[: int(case.get("noise") or 0)]):
                await facts_collection.add_fact(
                    user_id,
                    SEED_SESSION,
                    {"category": "noise", "subject": f"噪声{i}", "content": text},
                )
            # hard negative：同领域、说法相近、但答案是错的 —— 真正考检索的地方
            for i, item in enumerate(case.get("distractors") or []):
                await facts_collection.add_fact(
                    user_id,
                    SEED_SESSION,
                    {
                        "category": item.get("category") or "noise",
                        "subject": item.get("subject") or f"干扰{i}",
                        "content": item["content"],
                    },
                )

        # ④ 入库检查：确认这条事实到底有没有进库
        stored = await _list_facts(user_id)
        row["stored_count"] = len(stored)
        row["in_store"] = any(
            any(kw.lower() in s.lower() for kw in expected) for s in stored
        )

        # ⑤ 检索 + ⑥ 打分
        docs = await _query_facts(user_id, case["question"], top_k, use_hybrid)
        rank = _rank_of(docs, expected)
        row["rank"] = rank
        row["top1"] = rank == 1
        row["mrr"] = (1.0 / rank) if rank else 0.0
        row["violation"] = _violates(docs, case.get("must_not") or [])
        # 保留原文，便于诊断"到底是没抽出来，还是抽错了"
        row["stored"] = stored
        row["retrieved"] = docs
    finally:
        await _cleanup(user_id)

    return row


# ==================== 报告 ====================

def _print_report(rows: List[Dict[str, Any]], mode: str, top_k: int) -> Dict[str, Any]:
    print(f"\n模式: {mode}    top_k={top_k}")
    print("-" * 66)
    print(f"{'用例':<10}{'类别':<12}{'干扰':<6}{'入库':<7}{'命中':<12}{'MRR':<8}{'违规':<6}")
    print("-" * 66)
    for r in rows:
        rank = r["rank"]
        hit_text = f"第 {rank} 名" if rank else "未命中"
        # 标记用 ASCII：Windows 控制台默认 GBK，打不出 ✓/✗/⚠
        print(
            f"{r['id']:<10}{r['category']:<12}"
            f"{r['distractor_count']:<6}"
            f"{('ok' if r['in_store'] else 'NO'):<7}"
            f"{hit_text:<12}"
            f"{r['mrr']:<8.2f}"
            f"{('!!' if r['violation'] else ''):<6}"
        )
    print("-" * 66)

    total = len(rows)
    stored = [r for r in rows if r["in_store"]]
    # 检索指标的分母只用"已入库"的用例：没抽出来的不该算检索的锅
    hits = [r for r in stored if r["rank"] is not None]
    top1 = [r for r in stored if r["top1"]]
    mrr = sum(r["mrr"] for r in stored) / len(stored) if stored else 0.0
    violations = [r for r in rows if r["violation"]]

    summary = {
        "mode": mode,
        "top_k": top_k,
        "cases": total,
        "extract_success": len(stored),
        "extract_rate": round(len(stored) / total, 3) if total else 0.0,
        "recall_at_k": len(hits),
        "recall_denominator": len(stored),
        "recall_rate": round(len(hits) / len(stored), 3) if stored else 0.0,
        "top1": len(top1),
        "top1_rate": round(len(top1) / len(stored), 3) if stored else 0.0,
        "mrr": round(mrr, 3),
        "violations": len(violations),
    }

    print(f"事实抽取成功率   {len(stored)}/{total}")
    print(f"检索 Recall@{top_k}    {len(hits)}/{len(stored)}   (分母=已入库的用例)")
    print(f"检索 Top-1 准确率 {len(top1)}/{len(stored)}   ← 有 hard negative 时这个才关键")
    print(f"检索 MRR          {mrr:.2f}")
    print(f"must_not 违规     {len(violations)}")
    if violations:
        print(f"  违规用例: {[v['id'] for v in violations]}")
    return summary


# ==================== 入口 ====================

async def main() -> int:
    parser = argparse.ArgumentParser(description="检索层（L1）评测")
    parser.add_argument(
        "--mode",
        choices=["retrieval", "full"],
        default="retrieval",
        help="retrieval=只测检索（跳过 LLM）；full=完整链路（含 LLM 抽取）",
    )
    parser.add_argument("--top-k", type=int, default=5, help="召回条数 K")
    parser.add_argument("--json", dest="json_path", default=None, help="结果写入 JSON 文件")
    parser.add_argument(
        "--dump", action="store_true", help="打印每个用例实际入库/召回的原文（诊断用）"
    )
    parser.add_argument(
        "--hybrid",
        action="store_true",
        help="开启轻量混合检索（分类软加权 + 关键词融合），与纯向量做 A/B",
    )
    args = parser.parse_args()

    if facts_collection is None:
        print("Chroma 事实集合不可用（连接失败？），无法评测")
        return 1

    if args.mode == "full" and not settings.SUB_MODEL_NAME:
        print("[!] 未配置 SUB_MODEL_NAME：full 模式不会有事实产出，建议先用 retrieval 模式")

    # 先说清楚用的是什么 embedding：中文 embedding 没配上时，
    # 检索分数低是 embedding 的锅，不是架构的锅
    ef = get_embedding_function()
    emb_desc = (
        f"{settings.EMBEDDING_MODEL}（硅基流动）"
        if ef is not None
        else "Chroma 默认英文模型 —— 未配置中文 embedding，中文召回会明显偏差"
    )
    print(f"embedding: {emb_desc}")

    noise_pool, cases = _load_cases()
    scanner = SessionScanner() if args.mode == "full" else None
    ts = int(time.time())

    rows: List[Dict[str, Any]] = []
    for case in cases:
        rows.append(
            await run_case(case, args.mode, args.top_k, noise_pool, scanner, ts, args.hybrid)
        )

    summary = _print_report(rows, args.mode, args.top_k)

    if args.dump:
        print("\n" + "=" * 66)
        print("明细（入库原文 / 召回原文）")
        print("=" * 66)
        for r in rows:
            print(f"\n[{r['id']}] {r['category']}  期望={r['expected']}")
            print(f"  入库 {r['stored_count']} 条:")
            for s in r["stored"]:
                print(f"    - {s}")
            print("  召回前 3 条:")
            for i, d in enumerate(r["retrieved"][:3], start=1):
                print(f"    {i}. {d}")

    if args.json_path:
        out = {"summary": summary, "rows": rows}
        Path(args.json_path).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入 {args.json_path}")

    # 默认总是生成结果文件（UTF-8，避免控制台 GBK 乱码），方便查看实际召回内容
    _write_result_files(rows, summary, args)

    return 0


def _write_result_files(rows: List[Dict[str, Any]], summary: Dict[str, Any], args) -> None:
    """写出人读 .txt 与机读 .json，含每题：入库原文 + 召回前 5 条带排名。"""
    out_dir = Path(__file__).resolve().parent

    # 机读
    json_path = out_dir / "eval_result.json"
    json_path.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 人读
    lines: List[str] = []
    lines.append(
        f"模式: {args.mode}  top_k={args.top_k}  hybrid={'on' if args.hybrid else 'off'}"
    )
    lines.append("=" * 72)
    for r in rows:
        expected = r.get("expected") or []
        lines.append("")
        lines.append(
            f"[{r['id']}] {r['category']}  期望={expected}  "
            f"入库={r['stored_count']}  命中rank={r['rank']}  top1={r['top1']}  违规={r['violation']}"
        )
        lines.append(f"  问题: {r.get('question', '')}")
        lines.append(f"  入库事实 {r['stored_count']} 条:")
        for s in r.get("stored") or []:
            lines.append(f"    - {s}")
        lines.append("  召回前 5 条:")
        for i, d in enumerate((r.get("retrieved") or [])[:5], start=1):
            hit = any(kw.lower() in d.lower() for kw in expected)
            lines.append(f"    {i}. {d}" + ("   <== 命中" if hit else ""))
    lines.append("")
    lines.append("=" * 72)
    lines.append(
        f"摘要: 抽取 {summary['extract_rate']}  "
        f"召回@{summary['top_k']} {summary['recall_rate']}  "
        f"Top1 {summary['top1_rate']}  MRR {summary['mrr']}  违规 {summary['violations']}"
    )
    txt_path = out_dir / "eval_result.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n结果已写入: {txt_path}  (机读: {json_path})")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
