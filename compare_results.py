#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_results.py - 比較資料夾內所有 result.*.json 與 expected.json。

供 run.bat 選項 5 呼叫，也可獨立執行：
    python compare_results.py                      # 比較所有 result.*.json
    python compare_results.py --baseline other.json
    python compare_results.py --show-diffs 5
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

from fentay_common import DEFAULT_OUT_DIR, compare, log, now_iso, validate

DEFAULT_BASELINE = "expected.json"
RESULT_PATTERN = "result.*.json"

# 報告來源說明，用於標示各結果的產生方式
SOURCE_LABELS = {
    "C": "C      - PDF 文字層直接解析（無 LLM）",
    "pure": "pure   - vision 模型讀圖並套用全部規則（測 Skill 指令是否有效）",
    "hybrid": "hybrid - vision 模型只讀表，規則由 Python 套用（實務做法）",
}


def describe(path):
    """從檔名推斷解析路線與工作模式，回傳可讀說明。"""
    stem = os.path.basename(path)
    for suffix, label in SOURCE_LABELS.items():
        if stem.endswith("." + suffix + ".json"):
            return label
    return "unknown"


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_results(out_dir):
    """
    在輸出目錄內尋找結果 JSON。

    JSON 使用固定檔名（result.C.json、result.<model>.<mode>.json），每次執行覆蓋，
    因此這裡只會找到最新一份。報告檔因為帶時間戳會有多份，但不參與比對。
    """
    pattern = os.path.join(out_dir, RESULT_PATTERN)
    return sorted(p for p in glob.glob(pattern)
                  if not p.endswith(".report.txt"))


def main():
    parser = argparse.ArgumentParser(description="比較所有解析結果與基準答案")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE,
                        help="基準答案 JSON，預設 %s" % DEFAULT_BASELINE)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help="結果目錄，預設 %s" % DEFAULT_OUT_DIR)
    parser.add_argument("--show-diffs", type=int, default=3,
                        help="每個檔案顯示的差異筆數")
    args = parser.parse_args()

    if not os.path.isfile(args.baseline):
        sys.exit("找不到基準答案: %s" % args.baseline)

    if not os.path.isdir(args.out_dir):
        sys.exit("找不到結果目錄 %s，請先執行解析（run.bat 選項 2 或 3）"
                 % args.out_dir)

    paths = find_results(args.out_dir)
    if not paths:
        sys.exit("%s 內找不到 %s，請先執行解析（run.bat 選項 2 或 3）"
                 % (args.out_dir, RESULT_PATTERN))

    baseline = load(args.baseline)
    print("=" * 78)
    print("比較時間: %s" % now_iso())
    print("基準答案: %s (%s 筆)" % (args.baseline, baseline.get("recordCount")))
    print("結果目錄: %s" % args.out_dir)
    print("=" * 78)

    summary = []
    for path in paths:
        try:
            actual = load(path)
        except (OSError, json.JSONDecodeError) as exc:
            print()
            print("[%s] 無法讀取: %s" % (os.path.basename(path), exc))
            continue

        problems = validate(actual)
        text = compare(baseline, actual, args.show_diffs)
        rate = ""
        for line in text.splitlines():
            if line.startswith("逐欄正確率"):
                rate = line.split(":")[-1].strip().split("(")[0].strip()

        meta = actual.get("_meta") or {}
        summary.append({
            "file": os.path.basename(path),
            "source": describe(path),
            "count": actual.get("recordCount"),
            "rate": rate,
            "issues": len(problems),
            "generated": meta.get("generatedAt", "-"),
            "elapsed": meta.get("elapsedSec"),
        })

        print()
        print("-" * 78)
        print("%s" % os.path.basename(path))
        print("  來源: %s" % describe(path))
        print("  產出時間: %s" % meta.get("generatedAt", "(無記錄)"))
        if meta.get("elapsedSec") is not None:
            print("  耗時: %.3f 秒" % meta["elapsedSec"])
        print("-" * 78)
        print(text)
        if problems:
            print()
            print("  驗證問題 (%d):" % len(problems))
            for item in problems[:10]:
                print("    - %s" % item)

    print()
    print("=" * 78)
    print("彙總")
    print("=" * 78)
    print("%-34s %-5s %-7s %-7s %s"
          % ("檔案", "筆數", "正確率", "耗時(秒)", "產出時間"))
    for item in sorted(summary, key=lambda s: (s["rate"] != "100.0%", s["file"])):
        elapsed = ("%.2f" % item["elapsed"]) if item["elapsed"] is not None else "-"
        print("%-34s %-5s %-7s %-7s %s"
              % (item["file"][:34], item["count"], item["rate"] or "n/a",
                 elapsed, item["generated"]))

    full = [s for s in summary if s["rate"] == "100.0%"]
    print()
    if full:
        print("達到 100%% 的結果: %s" % ", ".join(s["file"] for s in full))
    print()
    print("提醒: expected.json 是由 PDF 文字層產生的，")
    print("      與路線 C 比對屬循環論證。路線 C 的獨立證據是其報告內的")
    print("      「QTY x PRICE vs 金額」交叉驗證，非此處的正確率。")
    log("")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
