"""查重规则离线评测。

用法：
    .venv\\Scripts\\python.exe tools/eval_dedup.py
    .venv\\Scripts\\python.exe tools/eval_dedup.py --verbose   # 逐条打印
    .venv\\Scripts\\python.exe tools/eval_dedup.py --case c22  # 只看某条

为什么要有它：
    查重规则不是"看着对"就行，得有 36 条带期望标签的样例兜住。改阈值、
    加同义词之后必须重跑，确认没有把原本对的判成错的（误杀比漏判贵得多：
    漏判只是多一条待确认，误杀是用户的任务直接消失）。

输出：
    * 准确率
    * 每个状态的 precision / recall / F1
    * **误杀清单**（期望 new/conflict/recurring 却被判成 duplicate）
    * 错判明细（含 score 与 reason，方便判断是规则错还是阈值错）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from agent import dedup  # noqa: E402

CASES = os.path.join(HERE, "dedup_cases.jsonl")
LABELS = ["duplicate", "similar", "conflict", "recurring", "new"]


def load_cases(path: str) -> list[dict]:
    cases = []
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{line_no} JSON 解析失败：{e}") from e
            case.setdefault("events", [])
            case.setdefault("pending", {"confirmed": [], "planned": []})
            cases.append(case)
    return cases


def run(cases: list[dict]) -> tuple[list[dict], Counter]:
    results = []
    confusion = Counter()
    for case in cases:
        got = dedup.classify(case["task"], case.get("events"), case.get("pending"))
        expect = case["expect"]
        actual = got["status"]
        confusion[(expect, actual)] += 1
        results.append({
            "id": case["id"],
            "desc": case.get("desc", ""),
            "expect": expect,
            "actual": actual,
            "score": got.get("score", 0.0),
            "reason": got.get("reason", ""),
            "match": got.get("match"),
        })
    return results, confusion


def prf(confusion: Counter, label: str) -> tuple[float, float, float, int]:
    tp = confusion[(label, label)]
    fp = sum(n for (e, a), n in confusion.items() if a == label and e != label)
    fn = sum(n for (e, a), n in confusion.items() if e == label and a != label)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1, tp + fp + fn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="打印每一条")
    parser.add_argument("--case", default="", help="只看某个 id")
    parser.add_argument("--file", default=CASES, help="样例集路径")
    args = parser.parse_args()

    cases = load_cases(args.file)
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    results, confusion = run(cases)

    total = len(results)
    correct = sum(1 for r in results if r["expect"] == r["actual"])

    print(f"样例 {total} 条，判对 {correct} 条，准确率 {correct / total:.1%}")
    print()
    print(f"{'状态':<10}{'precision':>10}{'recall':>9}{'f1':>8}{'样本':>7}")
    for label in LABELS:
        p, r, f1, n = prf(confusion, label)
        if n:
            print(f"{label:<10}{p:>10.2f}{r:>9.2f}{f1:>8.2f}{n:>7}")

    if not args.case:
        # 误杀：本应是新任务/冲突/周期，却被判成"已存在"
        kills = [r for r in results
                 if r["actual"] == "duplicate" and r["expect"] in ("new", "conflict", "recurring")]
        print()
        print(f"误杀（把新任务判成重复）：{len(kills)} 条")
        for r in kills:
            print(f"  {r['id']} {r['desc']}: 期望 {r['expect']}，实际 duplicate "
                  f"(score={r['score']}, {r['reason']})")

    misses = [r for r in results if r["expect"] != r["actual"]]
    if misses:
        print()
        print(f"错判明细：{len(misses)} 条")
        for r in misses:
            print(f"  {r['id']} {r['desc']}")
            print(f"      期望 {r['expect']:<9} 实际 {r['actual']:<9} "
                  f"score={r['score']} reason={r['reason']}")

    if args.verbose:
        print()
        for r in results:
            flag = "OK " if r["expect"] == r["actual"] else "BAD"
            print(f"{flag} {r['id']} {r['expect']:<9}->{r['actual']:<9} "
                  f"score={r['score']:<5} {r['desc']}")

    return 0 if correct == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
