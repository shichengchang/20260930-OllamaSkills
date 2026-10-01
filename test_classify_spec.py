#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_classify_spec.py - 驗證 FENTAY_B2B_CLASSIFY 規格的可實作性。

目的
----
FENTAY_B2B_CLASSIFY 描述「AI 只判欄位語意、程式碼負責其餘」的分工。
但該規格尚未接軌到任何程式碼，本腳本用來回答一個問題：

    這份規格描述的能力，AI 實際上能做到嗎？

做法
----
以 SKILL.md + MAPPING.md + EXAMPLES.md 三份文件的**實際內容**組出 prompt，
餵給本地 Ollama，檢查輸出的 mapping 是否正確。

測試分成三組，對應三種難度：
  A. EXAMPLES.md 內建的 3 個範例（規格自己給的答案，屬於「有標準答案可對」）
  B. 規格宣稱能處理、但程式碼目前做不到的情境（無表頭、欄位順序打亂、欄位數不等於 10）
  C. 真實豐泰.pdf 的資料（與既有實驗 3 對照）

若 A 組就失敗，代表規格本身有問題；
若 A 通過而 B 失敗，代表規格的宣稱過於樂觀；
若 A、B 都通過，代表規格可實作，且能補足程式碼的缺口。

本腳本只做唯讀的 AI 呼叫與計分，不修改 FENTAY_B2B_CLASSIFY 的任何檔案。
"""

import argparse
import glob
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，請執行: pip install requests")

import importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(BASE_DIR, "FENTAY_B2B_CLASSIFY")


def load(name):
    path = os.path.join(BASE_DIR, name)
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_skill_doc(name):
    path = os.path.join(SKILL_DIR, name)
    if not os.path.isfile(path):
        sys.exit("找不到 %s" % path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def call_ollama(host, model, prompt, num_predict, timeout):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {
            "temperature": 0,
            "num_predict": num_predict,
            "num_ctx": 32768,
        },
    }
    started = time.time()
    response = requests.post("%s/api/generate" % host, json=payload, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    return {
        "text": body.get("response", ""),
        "elapsed": round(time.time() - started, 1),
        "prompt_tokens": body.get("prompt_eval_count"),
        "eval_tokens": body.get("eval_count"),
    }


def build_prompt(skill_md, mapping_md, examples_md, columns, headers=None,
                 samples=None, include_examples=True):
    """
    以三份文件的實際內容組出 prompt。

    刻意不做任何摘要或改寫：規格實際上會餵給模型的內容就是這些。
    若為了讓測試通過而改寫文件內容，測試就失去意義。
    """
    parts = [skill_md, "", "=" * 60, "", mapping_md]
    if include_examples:
        parts += ["", "=" * 60, "", examples_md]

    parts += ["", "=" * 60, "",
              "以上是規格文件。以下是實際要判斷的表格。", ""]

    parts.append("欄位數 N = %d" % columns)
    parts.append("")

    if headers:
        parts.append("表頭:")
        parts.append(" | ".join("%d=%s" % (i, h) for i, h in enumerate(headers)))
        parts.append("")

    rows = samples or []
    if rows:
        parts.append("樣本資料:")
        for i, row in enumerate(rows):
            cells = []
            for k in range(columns):
                cells.append("%d=%s" % (k, row.get(k, "")))
            parts.append("R%d: %s" % (i, " | ".join(cells)))
        parts.append("")

    parts.append("請判斷每一欄代表哪個資料庫欄位，只回傳 mapping。")
    parts.append('格式: {"mapping": [...]}，長度必須等於 %d' % columns)
    return "\n".join(parts)


def parse_mapping(text, n_columns):
    """解析 AI 回應的 mapping，並檢查長度與型別。"""
    try:
        obj = json.loads(text)
    except Exception:
        return None, "JSON 解析失敗: %s" % text[:80]
    if not isinstance(obj, dict) or "mapping" not in obj:
        return None, "缺少 mapping 鍵: %s" % text[:80]
    mapping = obj["mapping"]
    if not isinstance(mapping, list):
        return None, "mapping 不是陣列"
    if len(mapping) != n_columns:
        return mapping, "長度 %d，預期 %d" % (len(mapping), n_columns)
    return mapping, None


def score_mapping(mapping, truth):
    """以欄位名比對計分，金額欄以 null 視為正確。"""
    if mapping is None or len(mapping) != len(truth):
        return 0.0, []
    wrong = []
    hit = 0
    for i, (a, e) in enumerate(zip(mapping, truth)):
        an = a if a is None else str(a)
        en = e if e is None else str(e)
        if an == en:
            hit += 1
        else:
            wrong.append((i, an, en))
    return hit / len(truth), wrong


# ------------------------------------------------------------------ 測試案例

def case_examples_1():
    """EXAMPLES.md 範例 1：無表頭，11 欄，含備註欄。"""
    rows = [
        {0: "6AF3101", 1: "7801K", 2: '44"3AB灰網布/(3AB)', 3: '44"', 4: "碼",
         5: "500.0", 6: "22.00", 7: "11,000.00", 8: "2026/06/12", 9: "急單", 10: "LU1"},
        {0: "6AF3102", 1: "7802K", 2: '60"3AC黃網布/(3AC)', 3: '60"', 4: "碼",
         5: "1,200.0", 6: "18.50", 7: "22,200.00", 8: "2026/06/19", 9: "", 10: "DS1"},
        {0: "6AF3103", 1: "7803K", 2: '44"3AD黑保利布/(3AD)', 3: '44"', 4: "公尺",
         5: "36.0", 6: "95.00", 7: "3,420.00", 8: "2026/06/19", 9: "", 10: "LU1"},
    ]
    truth = ["ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "UNIT", "QTY",
             "PRICE", None, "NEED_DATE", None, "NEED_CUST"]
    return {"name": "範例1 無表頭 11 欄含備註", "columns": 11,
            "headers": None, "rows": rows, "truth": truth}


def case_examples_2():
    """EXAMPLES.md 範例 2：無表頭，8 欄且順序打亂，缺 MATM_NAME 與 WIDE。"""
    rows = [
        {0: "1,460.0", 1: "碼", 2: "12.50", 3: "18,250.00", 4: "6AF2001",
         5: '56"P28黑特利', 6: "2026/05/08", 7: "DS1"},
        {0: "320.0", 1: "碼", 2: "8.00", 3: "2,560.00", 4: "6AF2002",
         5: '44"1BC藍網布/(1BC)', 6: "2026/05/15", 7: "LU1"},
        {0: "75.0", 1: "公尺", 2: "41.20", 3: "3,090.00", 4: "6AF2003",
         5: '60"2DE白尼龍布/(2DE)', 6: "2026/05/22", 7: "DS1"},
    ]
    truth = ["QTY", "UNIT", "PRICE", None, "ORD_NO", "MATM_DESC",
             "NEED_DATE", "NEED_CUST"]
    return {"name": "範例2 無表頭 8 欄順序打亂", "columns": 8,
            "headers": None, "rows": rows, "truth": truth}


def case_header_shuffled():
    """
    有表頭且欄位順序完全打亂 —— MAPPING.md 第 6~7 行明確宣稱的情境：
    「判斷以表頭文字為第一依據…與欄位的排列順序無關」。

    這是原測試設計的缺口：只測了「無表頭+打亂」與「有表頭+預設順序」
    兩個極端，沒有測兩者的交點。少了這個案例，無法判斷規格的宣稱是否成立。

    資料取自真實豐泰.pdf 第 1 頁，但欄位順序重排為
    [子公司, 交貨日期, 金額, 單價, 數量, 規格, 料號, 單位, 訂購單號, 材料名稱]
    —— 刻意讓 MATM_NAME 與 ORD_NO 互換位置，測試 AI 是否會被
    「左側通常是單號」的直覺誤導（這是 MAPPING.md 自己都承認的弱證據）。
    """
    headers = ["子公司", "交貨日期", "金      額", "單     價", "數     量",
               "規    格", "料 號", "單位", "訂購單號", "材料名稱/顏色代碼"]
    rows = [
        {0: "LU1", 1: "2026/03/27", 2: "59,860.00", 3: "73.00", 4: "820.0",
         5: '44"', 6: "3056G", 7: "碼", 8: "6AF1101",
         9: '44"74F黃LJ-A8-P網布/(74F)'},
        {0: "LU1", 1: "2026/03/27", 2: "1,566.00", 3: "29.00", 4: "54.0",
         5: '44"', 6: "307LJ", 7: "碼", 8: "6AF1102",
         9: '44"0AH灰LJ-B4網布/(0AH)'},
        {0: "DS1", 1: "2026/04/10", 2: "208.00", 3: "104.00", 4: "2.0",
         5: '40"', 6: "309QS", 7: "碼", 8: "6AF1103",
         9: '40"10A LJA8P網水性漿/(10A)'},
    ]
    # 表頭在欄位 6 是「料 號」-> MATM_NAME，欄位 8 是「訂購單號」-> ORD_NO
    truth = ["NEED_CUST", "NEED_DATE", None, "PRICE", "QTY", "WIDE",
             "MATM_NAME", "UNIT", "ORD_NO", "MATM_DESC"]
    return {"name": "有表頭 10 欄順序完全打亂", "columns": 10,
            "headers": headers, "rows": rows, "truth": truth}


def case_header_shuffled_swapped():
    """
    同上有表頭打亂，但額外讓 MATM_NAME 與 ORD_NO 互換位置。

    兩者都是短英數代碼、幾乎不重複、長度相近，是 MAPPING.md
    第 63~73 行承認的「無法可靠區分」組合。此案例檢查 AI 是否
    會被位置直覺誤導，以及在無法區分時是否誠實地填 null。
    """
    headers = ["訂購單號", "材料名稱/顏色代碼", "單     價", "單位", "規格",
               "料 號", "交貨日期", "數     量", "金      額", "子公司"]
    rows = [
        {0: "6AF1101", 1: '44"74F黃LJ-A8-P網布/(74F)', 2: "73.00", 3: "碼",
         4: '44"', 5: "3056G", 6: "2026/03/27", 7: "820.0",
         8: "59,860.00", 9: "LU1"},
        {0: "6AF1102", 1: '44"0AH灰LJ-B4網布/(0AH)', 2: "29.00", 3: "碼",
         4: '44"', 5: "307LJ", 6: "2026/03/27", 7: "54.0",
         8: "1,566.00", 9: "LU1"},
        {0: "6AF1103", 1: '40"10A LJA8P網水性漿/(10A)', 2: "104.00", 3: "碼",
         4: '40"', 5: "309QS", 6: "2026/04/10", 7: "2.0",
         8: "208.00", 9: "DS1"},
    ]
    # 訂購單號刻意放在第 0 欄、料號放在第 5 欄，與常見「料號在前」相反。
    # 表頭文字明確，因此正確答案應完全依表頭判斷。
    truth = ["ORD_NO", "MATM_DESC", "PRICE", "UNIT", "WIDE",
             "MATM_NAME", "NEED_DATE", "QTY", None, "NEED_CUST"]
    return {"name": "有表頭 料號延後至第5欄", "columns": 10,
            "headers": headers, "rows": rows, "truth": truth}


def case_examples_3():
    """EXAMPLES.md 範例 3：英文表頭，9 欄。"""
    rows = [
        {0: "PO-88120", 1: "A100", 2: 'Mesh fabric 44" black', 3: '44"',
         4: "2,000.0", 5: "YD", 6: "3.250", 7: "6,500.00", 8: "2026-07-01"},
        {0: "PO-88121", 1: "A101", 2: 'Nylon 60" navy', 3: '60"',
         4: "450.0", 5: "YD", 6: "7.800", 7: "3,510.00", 8: "2026-07-08"},
    ]
    headers = ["PO Number", "Item No", "Description", "Width", "Qty",
               "Unit", "Unit Price", "Amount", "Delivery Date"]
    truth = ["ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "QTY", "UNIT",
             "PRICE", None, "NEED_DATE"]
    return {"name": "範例3 英文表頭 9 欄", "columns": 9,
            "headers": headers, "rows": rows, "truth": truth}


def case_hard_no_header():
    """
    規格宣稱能處理、但 pdf_text_fentay.infer_fields() 目前做不到的情境。

    12 欄、無表頭、順序完全打亂、欄位數不等於 10，
    且刻意讓 QTY 與 PRICE 的數值大小相反（數量小、單價高），
    檢查 MAPPING.md 所述的消歧規則是否真的有效。

    兩個額外欄位都填 null（備註、小計），因為規格規定同一欄位名
    最多出現一次，重複欄位本身就有歧義，不適合當測試題。
    """
    rows = [
        {0: "9800", 1: "154.00", 2: "PC", 3: "6AF9901", 4: "38\"9K白網布/(9K)",
         5: "125.0", 6: "2026/08/01", 7: '38"', 8: "DS2", 9: "急單", 10: "38\"", 11: "45,900.00"},
        {0: "9801", 1: "784.00", 2: "PC", 3: "6AF9902", 4: '52"3BK灰網/(3BK)',
         5: "80.0", 6: "2026/08/08", 7: '52"', 8: "DS1", 9: "", 10: '52"', 11: "62,720.00"},
        {0: "9802", 1: "104.00", 2: "PC", 3: "6AF9903", 4: '60"12B黑PE網/(12B)',
         5: "2.0", 6: "2026/08/15", 7: '60"', 8: "DS1", 9: "", 10: '60"', 11: "208.00"},
    ]
    truth = ["MATM_NAME", "PRICE", "UNIT", "ORD_NO", "MATM_DESC",
             "QTY", "NEED_DATE", "WIDE", "NEED_CUST", None, "WIDE_2", None]
    # WIDE 不應重複出現，第二個 38"/52"/60" 欄是同寬度的重複欄位，
    # 規格要求欄位名不重複，因此該欄應填 null 而非 WIDE
    truth[10] = None
    return {"name": "難題 無表頭 12 欄全打亂", "columns": 12,
            "headers": None, "rows": rows, "truth": truth}


def case_real_pdf():
    """真實豐泰.pdf 第 1 頁的資料，與既有實驗 3 對照。"""
    pt = load("pdf_text_fentay.py")
    try:
        import fitz
    except ImportError:
        sys.exit("缺少 PyMuPDF")
    pdfs = glob.glob(os.path.join(BASE_DIR, "*.pdf"))
    if not pdfs:
        sys.exit("找不到 PDF")
    doc = fitz.open(pdfs[0])
    spans = pt.collect_spans(doc[0])
    doc.close()

    top, bottom, _ = pt.find_table_bounds(spans)
    lefts, source = pt.detect_columns(spans, top, bottom, False)
    truth = pt.infer_fields(lefts, spans, top, len(lefts))
    rows = pt.cluster_rows(spans, top, bottom)

    samples = []
    for cells in rows[:3]:
        raw = {}
        for x, t in cells:
            raw.setdefault(pt.column_index(lefts, x), []).append(t)
        samples.append({k: "".join(v) for k, v in sorted(raw.items())})

    return {"name": "真實豐泰.pdf 第1頁", "columns": len(lefts),
            "headers": None, "rows": samples, "truth": truth,
            "detect_source": source}


GROUPS = [
    ("A 規格自帶範例（應完全正確）",
     [case_examples_1, case_examples_2, case_examples_3]),
    ("B 有表頭且順序打亂（規格明文宣稱與順序無關）",
     [case_header_shuffled, case_header_shuffled_swapped]),
    ("C 無表頭且順序打亂（程式碼做不到的情境）",
     [case_hard_no_header]),
    ("D 真實 PDF 對照",
     [case_real_pdf]),
]


def main():
    parser = argparse.ArgumentParser(
        description="驗證 FENTAY_B2B_CLASSIFY 規格的可實作性")
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--no-examples", action="store_true",
                        help="prompt 不含 EXAMPLES.md，用來量測其貢獻")
    parser.add_argument("--only", default=None,
                        help="只跑指定群組，例如 A / B / C")
    parser.add_argument("--dump-prompt", action="store_true")
    args = parser.parse_args()

    skill_md = read_skill_doc("SKILL.md")
    mapping_md = read_skill_doc("MAPPING.md")
    examples_md = read_skill_doc("EXAMPLES.md")

    print("=" * 68)
    print("FENTAY_B2B_CLASSIFY 規格可實作性測試")
    print("模型: %s   每案例執行 %d 次" % (args.model, args.runs))
    print("prompt 組成: SKILL.md + MAPPING.md + %s"
          % ("(不含 EXAMPLES.md)" if args.no_examples else "EXAMPLES.md"))
    print("=" * 68)

    results = []
    for group_name, builders in GROUPS:
        if args.only and not group_name.startswith(args.only):
            continue
        print("")
        print("### %s" % group_name)
        for builder in builders:
            case = builder()
            truth = case["truth"]
            n = case["columns"]

            prompt = build_prompt(
                skill_md, mapping_md, examples_md, n,
                headers=case["headers"], samples=case["rows"],
                include_examples=not args.no_examples)
            if args.dump_prompt:
                print("-" * 60)
                print(prompt)
                print("-" * 60)

            scores = []
            times = []
            ptoks = []
            outputs = []
            problems = []
            for _ in range(args.runs):
                resp = call_ollama(args.host, args.model, prompt, 700, args.timeout)
                times.append(resp["elapsed"])
                ptoks.append(resp["prompt_tokens"] or 0)
                mapping, problem = parse_mapping(resp["text"], n)
                if problem:
                    problems.append(problem)
                score, wrong = score_mapping(mapping, truth)
                scores.append(score)
                outputs.append((mapping, wrong))

            avg = sum(scores) / len(scores)
            same = len({json.dumps(m[0], ensure_ascii=False)
                        for m, _ in outputs}) == 1
            tag = "OK  " if avg == 1.0 else "FAIL"
            print("")
            print("  [%s] %s" % (tag, case["name"]))
            print("        欄位數=%d 得分=%.3f  %d 次一致=%s  平均 %.1fs  prompt %d tok"
                  % (n, avg, args.runs, "是" if same else "否",
                     sum(times) / len(times), int(sum(ptoks) / len(ptoks))))
            mapping, wrong = outputs[0]
            if wrong:
                print("        期望: %s" % truth)
                print("        實際: %s" % mapping)
                for i, a, e in wrong:
                    print("          欄%-2d 得 %-12s 應 %s" % (i, a, e))
            if problems:
                print("        格式問題: %s" % problems[0])

            results.append({
                "group": group_name, "name": case["name"],
                "columns": n, "score": avg,
                "consistent": same, "elapsed": sum(times) / len(times),
                "prompt_tokens": int(sum(ptoks) / len(ptoks)),
                "truth": truth,
                "got": outputs[0][0],
                "wrong": outputs[0][1],
            })

    print("")
    print("=" * 68)
    print("彙總")
    print("=" * 68)
    print("%-34s %6s %8s %6s" % ("案例", "得分", "耗時s", "一致"))
    for r in results:
        print("%-34s %6.3f %8.1f %6s"
              % (r["name"], r["score"], r["elapsed"],
                 "是" if r["consistent"] else "否"))
    perfect = sum(1 for r in results if r["score"] == 1.0)
    print("")
    print("完全正確: %d / %d" % (perfect, len(results)))
    print("（未達 1.000 的案例已在上方列出錯誤欄位）")

    out_path = os.path.join(BASE_DIR, "output", "classify_spec_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "runs": args.runs,
                   "include_examples": not args.no_examples,
                   "results": results}, f, ensure_ascii=False, indent=2)
    print("結果已寫入: %s" % out_path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()