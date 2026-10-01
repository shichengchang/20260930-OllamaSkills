#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_classify_spec.py - 驗證 FENTAY_B2B_CLASSIFY 規格，並比較 4 組 prompt 的準確度。

實驗目的
--------
回答兩個問題：

  1. 這份規格描述的能力，AI 實際上能做到嗎？
  2. 餵給模型多少規格文件才夠？   <- 這是 4 組 arm 比較的目標

4 組 arm（每組只增加一份文件，用來歸因每份文件的貢獻）
------------------------------------------------------------------
  A0 vocab   只有欄位詞彙清單 + 不匯入提示 + 輸出格式要求
  A1 mapping 只有 MAPPING.md（別名、內容特徵、消歧規則）
  A2 skill   SKILL.md + MAPPING.md（多了任務邊界與輸出契約）
  A3 full    三份全讀（加上 EXAMPLES.md 的 few-shot 範例）

汙染警告
--------
EXAMPLES.md 內含 3 個範例，而其中 3 個正是本腳本的測試案例
（case_examples_1/2/3）。對 A3 arm 而言這等於開卷考答案，
**不可用於 arm 比較**。每個案例都標記 contaminated，
彙總時只以乾淨案例計算 arm 排名。

Ground truth
------------
- 合成案例（layout_tests/）：正確答案在產生 PDF 時即確定，可靠
- 真實 PDF（PDF/ 與豐泰.pdf）：由 infer_fields() 推導，並用
  QTY x PRICE = PDF 金額 的交叉驗證檢查；驗證不通過者標記為可疑

用法
----
    python test_classify_spec.py                      # 全部案例 x 4 組 arm
    python test_classify_spec.py --arms A0 A1         # 只跑指定 arm
    python test_classify_spec.py --only D             # 只跑某群案例
    python test_classify_spec.py --model gemma4:12b   # 換模型
    python test_classify_spec.py --runs 5             # 增加重複次數
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
PDF_DIR = os.path.join(BASE_DIR, "PDF")


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


# ------------------------------------------------------------ 欄位詞彙（A0）

def build_vocab_prompt_text():
    """
    A0 的最小 prompt：只有欄位詞彙與輸出格式要求，不含任何規則。

    詞彙清單由 fentay_common.HEADER_ALIASES 的鍵取得，不另外硬編一份，
    避免與程式碼中的實際欄位集不同步。
    """
    fc = load("fentay_common.py")
    names = sorted(f for f in fc.HEADER_ALIASES if f)
    vocab = "、".join("`%s`" % n for n in names)
    return (
        "可用的資料庫欄位名稱：%s、null\n\n"
        "金額、小計、合計等不匯入資料庫的欄位，填 null。\n"
        "BRAND_NO、CUST_NO、CONTACT_NO 不出現在訂單表格中，不使用。\n\n"
        "請依每欄的內容判斷它代表哪個欄位，無法判斷者填 null。"
        % vocab
    )


# ---------------------------------------------------------------- Ollama 呼叫

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


# ------------------------------------------------------------- prompt 組裝

def build_prompt(arm, docs, columns, headers=None, samples=None):
    """
    依 arm 決定放入哪些規格文件。

    docs: {"skill": str, "mapping": str, "examples": str, "vocab": str}
    刻意不做摘要或改寫：規格實際餵給模型的內容就是文件全文。
    若為了讓測試通過而改寫文件，測試就失去意義。
    """
    parts = []
    if arm == "A0":
        parts.append(docs["vocab"])
    elif arm == "A1":
        parts.append(docs["mapping"])
    elif arm == "A2":
        parts += [docs["skill"], "", "=" * 60, "", docs["mapping"]]
    elif arm == "A3":
        parts += [docs["skill"], "", "=" * 60, "", docs["mapping"],
                  "", "=" * 60, "", docs["examples"]]
    else:
        sys.exit("未知的 arm: %s" % arm)

    parts += ["", "=" * 60, "", "以上是規格文件。以下是實際要判斷的表格。", ""]
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
            cells = ["%d=%s" % (k, row.get(k, "")) for k in range(columns)]
            parts.append("R%d: %s" % (i, " | ".join(cells)))
        parts.append("")

    parts.append("請判斷每一欄代表哪個資料庫欄位，只回傳 mapping。")
    parts.append('格式: {"mapping": [...]}，長度必須等於 %d' % columns)
    return "\n".join(parts)


ARMS = ["A0", "A1", "A2", "A3"]
ARM_DESC = {
    "A0": "僅欄位詞彙",
    "A1": "僅 MAPPING.md",
    "A2": "SKILL + MAPPING",
    "A3": "三份全讀",
}


# ------------------------------------------------------------------ 解析計分

def parse_mapping(text, n_columns):
    """解析 AI 回應的 mapping。回傳 (mapping, 違規清單)。"""
    violations = []
    try:
        obj = json.loads(text)
    except Exception:
        return None, ["JSON 解析失敗"]
    if not isinstance(obj, dict) or "mapping" not in obj:
        return None, ["缺少 mapping 鍵"]
    mapping = obj["mapping"]
    if not isinstance(mapping, list):
        return None, ["mapping 不是陣列"]

    if len(mapping) != n_columns:
        violations.append("長度 %d != %d" % (len(mapping), n_columns))

    # 字串 "null" 與 JSON null 語意不同，程式端若用真值判斷會誤判
    for i, v in enumerate(mapping):
        if isinstance(v, str) and v.strip().lower() == "null":
            violations.append("欄%d 使用字串 \"null\" 而非 JSON null" % i)

    named = [v for v in mapping if isinstance(v, str) and v.strip().lower() != "null"]
    dup = {x for x in named if named.count(x) > 1}
    if dup:
        violations.append("欄位名重複: %s" % ", ".join(sorted(dup)))
    return mapping, violations


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
#
# 每個案例回傳的 dict 需含：
#   name, columns, headers, rows, truth
#   contaminated : True 表示此案例的答案出現在 EXAMPLES.md 中，
#                  A3 arm 對它等於開卷，不可列入 arm 比較

def case_examples_1():
    """EXAMPLES.md 範例 1：無表頭，11 欄，含備註欄。答案在 EXAMPLES.md 中。"""
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
            "headers": None, "rows": rows, "truth": truth,
            "contaminated": True}


def case_examples_2():
    """EXAMPLES.md 範例 2：無表頭，8 欄且順序打亂。答案在 EXAMPLES.md 中。"""
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
            "headers": None, "rows": rows, "truth": truth,
            "contaminated": True}


def case_examples_3():
    """EXAMPLES.md 範例 3：英文表頭，9 欄。答案在 EXAMPLES.md 中。"""
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
            "headers": headers, "rows": rows, "truth": truth,
            "contaminated": True}


def case_header_shuffled():
    """有表頭且欄位順序完全打亂；MAPPING.md 明文宣稱與順序無關。乾淨案例。"""
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
    truth = ["NEED_CUST", "NEED_DATE", None, "PRICE", "QTY", "WIDE",
             "MATM_NAME", "UNIT", "ORD_NO", "MATM_DESC"]
    return {"name": "有表頭 10 欄順序完全打亂", "columns": 10,
            "headers": headers, "rows": rows, "truth": truth,
            "contaminated": False}


def case_header_shuffled_swapped():
    """有表頭，料號延後至第 5 欄。測試 AI 是否被位置直覺誤導。乾淨案例。"""
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
    truth = ["ORD_NO", "MATM_DESC", "PRICE", "UNIT", "WIDE",
             "MATM_NAME", "NEED_DATE", "QTY", None, "NEED_CUST"]
    return {"name": "有表頭 料號延後至第5欄", "columns": 10,
            "headers": headers, "rows": rows, "truth": truth,
            "contaminated": False}


def case_hard_no_header():
    """
    無表頭且 12 欄完全打亂，含兩個不匯入欄。
    刻意讓 QTY 小、PRICE 高，測試 MAPPING.md 的消歧規則是否有效。乾淨案例。
    """
    rows = [
        {0: "9800", 1: "154.00", 2: "PC", 3: "6AF9901", 4: '38"9K白網布/(9K)',
         5: "125.0", 6: "2026/08/01", 7: '38"', 8: "DS2", 9: "急單",
         10: '38"', 11: "45,900.00"},
        {0: "9801", 1: "784.00", 2: "PC", 3: "6AF9902", 4: '52"3BK灰網/(3BK)',
         5: "80.0", 6: "2026/08/08", 7: '52"', 8: "DS1", 9: "",
         10: '52"', 11: "62,720.00"},
        {0: "9802", 1: "104.00", 2: "PC", 3: "6AF9903", 4: '60"12B黑PE網/(12B)',
         5: "2.0", 6: "2026/08/15", 7: '60"', 8: "DS1", 9: "",
         10: '60"', 11: "208.00"},
    ]
    truth = ["MATM_NAME", "PRICE", "UNIT", "ORD_NO", "MATM_DESC",
             "QTY", "NEED_DATE", "WIDE", "NEED_CUST", None, None, None]
    return {"name": "難題 無表頭 12 欄全打亂", "columns": 12,
            "headers": None, "rows": rows, "truth": truth,
            "contaminated": False}


def case_real_pdf():
    """原始豐泰.pdf 第 1 頁，作為基準對照（不含表頭）。"""
    return _pdf_case(os.path.join(BASE_DIR, "豐泰.pdf"), "豐泰.pdf",
                     False, with_headers=False)


def case_real_pdf_headers():
    """原始豐泰.pdf，額外提供各欄表頭。"""
    return _pdf_case(os.path.join(BASE_DIR, "豐泰.pdf"), "豐泰.pdf",
                     False, with_headers=True)


def _extract_column_headers(spans, top, lefts, y_window=25, tol=None):
    """
    依欄位左界取出各欄的表頭文字。

    y_window 限制在資料區上界前若干 pt 之內，用來排除頁首抬頭
    （供應商、地址、列印編號）。不設限會把 '致  供應商:' 當成
    第一欄的表頭。

    已知限制：本 PDF 的「訂購單號」與「料 號」被排在同一個 span（x=21），
    因此 x=66 那一欄取不到自己的表頭，會錯抓右側欄位的表頭。
    這是 PDF 層資訊遺失，非本函式的判斷錯誤，故不做修補而如實回傳，
    由測試結果反映其影響。
    """
    if tol is None:
        pt = load("pdf_text_fentay.py")
        tol = pt.HEADER_X_TOLERANCE
    ymin = top - y_window
    out = []
    for left in lefts:
        best = None
        for y, x, t in spans:
            if y < ymin or y >= top or is_rule_text(t):
                continue
            d = abs(x - left)
            if d < tol and (best is None or d < best[0]):
                best = (d, t)
        out.append(best[1] if best else None)
    return out


def is_rule_text(text):
    """與 pdf_text_fentay.is_rule 相同，但避免重複載入模組。"""
    s = text.strip()
    return len(s) >= 4 and set(s) == {"-"}


def _pdf_case(path, label, contaminated, with_headers=False):
    """
    由真實 PDF 抽出樣本列與欄位對應。

    ground truth 來自 infer_fields()，並以 QTY x PRICE = PDF 金額
    的獨立交叉驗證檢查。若驗證不通過，代表基準本身可疑，會在結果中標記。

    with_headers: 是否一併提供各欄表頭文字給模型。
    """
    pt = load("pdf_text_fentay.py")
    try:
        import fitz
    except ImportError:
        sys.exit("缺少 PyMuPDF")

    doc = fitz.open(path)
    spans = pt.collect_spans(doc[0])
    doc.close()

    top, bottom, _ = pt.find_table_bounds(spans)
    if top is None:
        return None
    lefts, source = pt.detect_columns(spans, top, bottom, False)
    truth = pt.infer_fields(lefts, spans, top, len(lefts))
    rows = pt.cluster_rows(spans, top, bottom)

    samples = []
    for cells in rows:
        raw = {}
        for x, t in cells:
            raw.setdefault(pt.column_index(lefts, x), []).append(t)
        merged = {k: "".join(v) for k, v in sorted(raw.items())}
        if 0 in merged:
            samples.append(merged)

    # 獨立交叉驗證：欄位若錯位，QTY 與 PRICE 會對到不同資料列
    recs, _diag = pt.extract(path)
    amts = pt.collect_amounts(path)
    verified, mismatches = pt.cross_validate(recs, amts)
    trustworthy = (not mismatches) and verified == len(recs)

    headers = None
    if with_headers:
        headers = _extract_column_headers(spans, top, lefts)

    name = label if not with_headers else "%s +表頭" % label
    return {"name": "%s (%d 列)" % (name, len(samples)),
            "columns": len(lefts), "headers": headers,
            "rows": samples[:12], "truth": truth,
            "contaminated": contaminated,
            "detect_source": source,
            "cross_verified": "%d/%d" % (verified, len(recs)),
            "trustworthy": trustworthy,
            "total_rows": len(samples)}


def pdf_cases(with_headers=False, limit=None):
    """PDF/ 目錄下的測試檔案。"""
    if not os.path.isdir(PDF_DIR):
        return []
    out = []
    for path in sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf"))):
        label = os.path.basename(path)
        case = _pdf_case(path, label, False, with_headers=with_headers)
        if case:
            out.append(case)
        if limit and len(out) >= limit:
            break
    return out


GROUPS = [
    ("A 規格自帶範例【汙染，不列入 arm 比較】",
     [case_examples_1, case_examples_2, case_examples_3], False, None),
    ("B 有表頭且順序打亂",
     [case_header_shuffled, case_header_shuffled_swapped], False, None),
    ("C 無表頭且順序打亂",
     [case_hard_no_header], False, None),
    ("D 真實 PDF（無表頭）",
     [case_real_pdf, case_real_pdf_headers], True, None),
    ("E 新增測試 PDF（無表頭）",
     None, True, ("content", None)),
    ("F 新增測試 PDF（有表頭）",
     None, True, ("headers", 3)),
]


def main():
    parser = argparse.ArgumentParser(
        description="FENTAY_B2B_CLASSIFY 規格可實作性 + 4 組 prompt arm 比較")
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--arms", nargs="+", default=ARMS,
                        help="要比較的 arm，例如 --arms A0 A1")
    parser.add_argument("--only", default=None,
                        help="只跑指定群組，例如 A / B / C / D / E")
    parser.add_argument("--samples", type=int, default=3,
                        help="給模型看幾列樣本")
    parser.add_argument("--dump-prompt", action="store_true")
    args = parser.parse_args()

    docs = {
        "skill": read_skill_doc("SKILL.md"),
        "mapping": read_skill_doc("MAPPING.md"),
        "examples": read_skill_doc("EXAMPLES.md"),
        "vocab": build_vocab_prompt_text(),
    }

    print("=" * 72)
    print("FENTAY_B2B_CLASSIFY 規格可實作性 + arm 比較")
    print("模型: %s   每案例 %d 次   樣本列數: %d" % (args.model, args.runs, args.samples))
    print("要比較的 arm: %s" % ", ".join("%s(%s)" % (a, ARM_DESC[a]) for a in args.arms))
    print("=" * 72)

    results = []
    for group_name, builders, _flag, param in GROUPS:
        if args.only and not group_name.startswith(args.only):
            continue
        if builders is None:
            mode, limit = param
            cases = pdf_cases(with_headers=(mode == "headers"), limit=limit)
            if not cases:
                print("")
                print("### %s" % group_name)
                print("  （找不到 PDF/ 目錄或其中無可用檔案，略過）")
                continue
        else:
            cases = []
            for b in builders:
                c = b()
                if c:
                    cases.append(c)

        print("")
        print("### %s" % group_name)
        for case in cases:
            n = case["columns"]
            rows = case["rows"][:args.samples]
            flag = "【汙染】" if case["contaminated"] else ""
            extra = ""
            if "cross_verified" in case:
                extra = "  交叉驗證 %s%s" % (
                    case["cross_verified"],
                    "" if case.get("trustworthy") else "  <== 基準可疑")

            per_arm = {}
            for arm in args.arms:
                prompt = build_prompt(arm, docs, n,
                                      headers=case["headers"], samples=rows)
                if args.dump_prompt and arm == args.arms[0]:
                    print("-" * 60)
                    print(prompt)
                    print("-" * 60)

                scores, times, ptoks, outs, viols = [], [], [], [], []
                for _ in range(args.runs):
                    resp = call_ollama(args.host, args.model, prompt, 700, args.timeout)
                    times.append(resp["elapsed"])
                    ptoks.append(resp["prompt_tokens"] or 0)
                    mapping, v = parse_mapping(resp["text"], n)
                    score, wrong = score_mapping(mapping, case["truth"])
                    scores.append(score)
                    outs.append(mapping)
                    viols.append(v)
                per_arm[arm] = {
                    "score": sum(scores) / len(scores),
                    "exact": all(s == 1.0 for s in scores),
                    "elapsed": sum(times) / len(times),
                    "tokens": int(sum(ptoks) / len(ptoks)),
                    "got": outs[0],
                    "wrong": score_mapping(outs[0], case["truth"])[1],
                    "violations": viols[0],
                    "consistent": len({json.dumps(o, ensure_ascii=False) for o in outs}) == 1,
                }

            print("")
            print("  %s %s%s" % (case["name"], flag, extra))
            print("        欄位數=%d  期望: %s" % (n, case["truth"]))
            for arm in args.arms:
                r = per_arm[arm]
                tag = "OK  " if r["exact"] else "FAIL"
                line = "        [%s] %s 完全正確=%-5s 欄位=%.3f %s %.1fs %4d tok" % (
                    tag, arm, r["exact"], r["score"],
                    "一致" if r["consistent"] else "不一致",
                    r["elapsed"], r["tokens"])
                print(line)
                if r["wrong"]:
                    for i, a, e in r["wrong"]:
                        print("                欄%-2d 得 %-12s 應 %s" % (i, a, e))
                if r["violations"]:
                    print("                格式違規: %s" % "; ".join(r["violations"]))

            results.append({
                "group": group_name, "name": case["name"],
                "columns": n, "contaminated": case["contaminated"],
                "trustworthy": case.get("trustworthy"),
                "cross_verified": case.get("cross_verified"),
                "truth": case["truth"],
                "arms": {a: per_arm[a] for a in args.arms},
            })

    # ------------------------------------------------------------ 彙總
    print("")
    print("=" * 72)
    print("arm 比較彙總")
    print("=" * 72)
    clean = [r for r in results if not r["contaminated"]]
    dirty = [r for r in results if r["contaminated"]]
    suspicious = [r for r in clean if r["trustworthy"] is False]

    def arm_stats(rows):
        out = {}
        for arm in args.arms:
            if not rows:
                continue
            exact = sum(1 for r in rows if r["arms"][arm]["exact"])
            field = sum(r["arms"][arm]["score"] for r in rows) / len(rows)
            toks = sum(r["arms"][arm]["tokens"] for r in rows) / len(rows)
            secs = sum(r["arms"][arm]["elapsed"] for r in rows) / len(rows)
            bad = sum(1 for r in rows if r["arms"][arm]["violations"])
            out[arm] = (exact, field, toks, secs, bad)
        return out

    print("")
    print("【乾淨案例】%d 個（可列入 arm 比較）" % len(clean))
    print("  %-5s %-10s %-10s %-9s %-8s %-8s"
          % ("arm", "完全正確", "欄位準確率", "prompt tok", "耗時s", "格式違規"))
    for arm, (exact, field, toks, secs, bad) in arm_stats(clean).items():
        print("  %-5s %-10s %-10.3f %-9.0f %-8.1f %d/%d"
              % (arm, "%d/%d" % (exact, len(clean)), field, toks, secs, bad, len(clean)))

    if dirty:
        print("")
        print("【汙染案例】%d 個（答案在 EXAMPLES.md 中，僅供參考，不列入比較）" % len(dirty))
        print("  %-5s %-10s %-10s" % ("arm", "完全正確", "欄位準確率"))
        for arm, (exact, field, toks, secs, bad) in arm_stats(dirty).items():
            print("  %-5s %-10s %-10.3f"
                  % (arm, "%d/%d" % (exact, len(dirty)), field))

    if suspicious:
        print("")
        print("【基準可疑】以下 PDF 的交叉驗證不通過，ground truth 可能本身有誤：")
        for r in suspicious:
            print("  - %s（交叉驗證 %s）" % (r["name"], r["cross_verified"]))

    out_path = os.path.join(BASE_DIR, "output", "classify_arm_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "runs": args.runs,
                   "arms": args.arms, "samples": args.samples,
                   "results": results}, f, ensure_ascii=False, indent=2, default=str)
    print("")
    print("結果已寫入: %s" % out_path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()