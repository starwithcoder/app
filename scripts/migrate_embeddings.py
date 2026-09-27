"""
迁移 Chroma 集合到新的 embedding 模型。

为什么必须迁移：
    集合里存的向量是「用某个 embedding 模型算出来的」。换模型后：
    - 维度可能不同（旧 all-MiniLM=384，新 bge-m3=1024）
    - 即使维度相同，新旧向量也处在**不同的语义空间**，混在一起检索结果无意义
    所以必须：用新模型把已有文档**重新编码**一遍，存进新集合。

用法：
    python scripts/migrate_embeddings.py            # 只迁移，不删旧集合（安全）
    python scripts/migrate_embeddings.py --drop-old # 迁移完成后删除旧集合

说明：
    - 迁移用的是「旧集合读出 documents → 写进新集合」，写入时新模型会自动重新编码
    - 迁移是幂等的（id 相同，重复跑会覆盖而不是追加）
    - 建议先不带 --drop-old 跑一次，确认新集合数据正确后再删旧集合
"""
import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

# 让脚本可以直接 python scripts/migrate_embeddings.py 运行
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings  # noqa: E402
from infrastructure.database.chromadb_client import (  # noqa: E402
    get_chromadb_client,
    get_embedding_function,
)
from infrastructure.logging.logger import logger  # noqa: E402

BATCH_SIZE = 64

# 旧集合名（用默认英文 embedding 建的那批）→ 新集合名（配置里指定的）
OLD_TO_NEW = [
    ("memories_collection", settings.MEMORIES_COLLECTION),
    ("summary_collection", settings.SUMMARY_COLLECTION),
    ("facts_collection", settings.FACTS_COLLECTION),
]


def _existing_names(client: Any) -> List[str]:
    try:
        return [c.name for c in client.list_collections()]
    except Exception:
        # 不同版本 chromadb 的 list_collections 返回值不同，兼容一下
        return [c.name for c in client.list_collections()]  # pragma: no cover


def migrate_one(client: Any, old_name: str, new_name: str, ef: Any, drop_old: bool) -> int:
    if old_name == new_name:
        print(f"  · {old_name}: 新旧同名，跳过")
        return 0

    existing = _existing_names(client)
    if old_name not in existing:
        print(f"  · {old_name}: 不存在，跳过")
        return 0
    if new_name in existing:
        print(f"  · {new_name}: 已存在，跳过（如需重跑请先手动删除）")
        return 0

    old = client.get_collection(old_name)
    data: Dict[str, Any] = old.get(include=["documents", "metadatas"])
    ids = data.get("ids") or []
    if not ids:
        print(f"  · {old_name}: 空集合，跳过")
        return 0

    new = client.get_or_create_collection(name=new_name, embedding_function=ef)

    documents = data.get("documents") or []
    metadatas = data.get("metadatas") or []
    total = 0
    for start in range(0, len(ids), BATCH_SIZE):
        end = start + BATCH_SIZE
        new.add(
            ids=ids[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )
        total += len(ids[start:end])

    print(f"  · {old_name} → {new_name}: 迁移 {total} 条")

    if drop_old:
        client.delete_collection(old_name)
        print(f"    已删除旧集合 {old_name}")

    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移 Chroma 集合到新的 embedding 模型")
    parser.add_argument(
        "--drop-old",
        action="store_true",
        help="迁移完成后删除旧集合（默认不删，更安全）",
    )
    args = parser.parse_args()

    ef = get_embedding_function()
    if ef is None:
        logger.error("embedding 函数未配置（缺 API Key / Base URL），无法迁移")
        sys.exit(1)

    client = get_chromadb_client().client
    if client is None:
        logger.error("Chroma 客户端不可用，无法迁移")
        sys.exit(1)

    print(f"embedding 模型: {settings.EMBEDDING_MODEL}")
    print("开始迁移：")
    grand_total = 0
    for old_name, new_name in OLD_TO_NEW:
        grand_total += migrate_one(client, old_name, new_name, ef, args.drop_old)

    print(f"\n完成，共迁移 {grand_total} 条")
    if not args.drop_old:
        print("提示：旧集合仍保留。确认新集合无误后，可加 --drop-old 再跑一次删除旧集合。")


if __name__ == "__main__":
    main()
