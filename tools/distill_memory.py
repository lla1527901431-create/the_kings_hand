"""手工执行「过期日程 → 记忆沉淀」。

用途：
    * 调试分桶与摘要效果（配合 --dry-run，只看不写）
    * 登录触发的自动沉淀因为没 Key / 调用失败而跳过时，补跑
    * 迁移历史数据

用法：
    python tools/distill_memory.py --phone 13800138000 --dry-run
    python tools/distill_memory.py --phone 13800138000
    python tools/distill_memory.py --all --dry-run
    python tools/distill_memory.py --phone 13800138000 --db data/other.db
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="把过期日程沉淀进长期记忆")
    parser.add_argument("--phone", default="", help="要处理的手机号")
    parser.add_argument("--all", action="store_true", help="处理白名单里的所有人")
    parser.add_argument("--db", default="", help="指定数据库路径（默认 data/app.db）")
    parser.add_argument("--dry-run", action="store_true", help="只计算，不写记忆、不删日程")
    parser.add_argument("--max-buckets", type=int, default=8, help="一次最多处理几个桶")
    args = parser.parse_args()

    if args.db:
        os.environ["DB_PATH"] = os.path.abspath(args.db)

    # ⚠️ 必须在设置 DB_PATH 之后再 import，config 在导入时就读环境变量
    from agent import memory_history
    from db import settings, whitelist

    if args.all:
        phones = [row["phone"] for row in whitelist.list_all()]
    elif args.phone:
        phones = [args.phone]
    else:
        parser.error("需要 --phone 或 --all")

    # 全站只有一把 Key（管理员在「访问管理」里配的），跟具体手机号无关
    key = settings.deepseek_key()
    if not key:
        print("[skip] 还没有配置全局 DeepSeek API Key（「访问管理」里填一次）")
        return 1

    source = "shared"
    failed = 0
    for phone in phones:
        try:
            result = memory_history.distill(
                phone, key, max_buckets=args.max_buckets, dry_run=args.dry_run)
        except Exception as e:                                # noqa: BLE001
            failed += 1
            print(f"[fail] {phone}：{type(e).__name__}: {e}")
            continue
        print(f"[ok] {phone}（key={source}）{json.dumps(result, ensure_ascii=False)}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
