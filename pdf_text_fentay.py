#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_text_fentay.py - 方案 C：直接解析 PDF 文字層，輸出 FENTAY_B2B 格式 JSON。

與 ollama_fentay.py（方案 B，vision 模型）的差異：
  - 不呼叫任何 LLM，速度為毫秒級
  - 無 OCR 失真（文字層即原始碼）
  - 僅適用於「有文字層」的 PDF；掃描件會明確報錯而非靜默失敗

設計要點：
  - MAPPING.md 的轉換規則與輸出契約全部 import 自 fentay_common.py，
    與方案 B 共用同一份實作，兩者比較才公平。
  - 本 PDF 為無框線表單，且文字層為「整欄連續輸出」而非逐列交錯，
    因此必須以座標分組成列，不可依賴文字輸出的先後順序。

用法：
    python pdf_text_fentay.py --pdf ".\\豐泰.pdf" \
        --compare ".\\expected.json" --dump-columns

結果預設寫入專案下的 output/。
"""

import argparse
import json
import os
import sys
import time

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("缺少 PyMuPDF，請執行: pip install PyMuPDF")

from fentay_common import (
    HEADER_ALIASES, apply_mapping, apply_ocr_fixes, build_output, compare, log,
    now_iso, normalize_key, resolve_out_dir, timestamp_slug, to_number, validate,
)

# PDF 表頭的實際欄位順序，用於第 3 層備援與欄位語意判定。
# None 代表該欄依 MAPPING.md 不匯入資料庫（「金額」欄）。
DOCUMENT_COLUMN_ORDER = [
    "ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "QTY",
    "UNIT", "PRICE", None, "NEED_DATE", "NEED_CUST",
]

# 「金額」欄在 DOCUMENT_COLUMN_ORDER 中的索引，供獨立交叉驗證使用
DOC_AMOUNT_INDEX = DOCUMENT_COLUMN_ORDER.index(None)

ROW_Y_TOLERANCE = 2  # pt，同一列的 span 可能差 1pt
HEADER_X_TOLERANCE = 40  # pt，表頭文字與欄位左界的最大可接受距離


def collect_spans(page):
    """取出頁面上所有非空白 span，回傳 [(y, x, text)]。"""
    spans = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if text:
                    spans.append((round(span["bbox"][1]), round(span["bbox"][0]), text))
    return spans


def is_rule(text):
    """
    判斷是否為表頭虛線（僅含 '-' 與空白，且 '-' 數量 >= 4）。

    容忍尾端空白：產生測試 PDF 時欄距不足會讓相鄰虛線被併入同一個
    span 且帶有空白字元，若不容忍空白會誤判為非虛線。
    """
    stripped = text.strip()
    return len(stripped) >= 4 and set(stripped) == {"-"}


def _guess_data_top_by_repetition(spans):
    """
    備援：當無法用絕對 x 門檻找到首欄時，改用「最左資料欄」定位資料列。

    處理的是**版面位移**（整張表往右挪動），不是單號格式改變。
    判斷依據是資料列的縱向重複性：同一 x 位置出現多列等距文字，
    且該欄不是表頭文字（表頭只有一列）。

    只看「最左欄」不足以分辨表頭與資料，因此要求該 x 至少出現 3 列；
    這一條同時排除了抬頭（供應商、地址，通常 1~2 筆）。
    """
    body = [(y, x, t) for y, x, t in spans if not is_rule(t) and t.strip()]
    if not body:
        return None

    counts = {}
    for y, x, _t in body:
        counts.setdefault(x, []).append(y)
    repeated = [ys for ys in counts.values() if len(ys) >= 3]
    if not repeated:
        return None

    # 資料區起點 = 重複欄位中最早的 y。
    # 表頭文字雖在最上方，但只出現一列，已被 >= 3 的條件排除。
    return min(min(ys) for ys in repeated)


def find_table_bounds(spans):
    """
    定位資料區上界（第一筆資料列）與下界（頁尾長虛線分隔線）。

    優先以「第一欄內容開頭為 6」定位（此格式由 FENTAY_B2B 文件定義）。
    失敗時改以最左資料欄的縱向重複性定位，避免使用絕對 x 門檻
    （如 x < 30）—— 該門檻會讓整張表右移後完全找不到資料區。
    """
    # 單號格式（開頭為 6）由 FENTAY_B2B 文件定義，但不綁定 x 座標，
    # 否則整張表右移就會漏判。
    starts = [y for y, x, t in spans if t[:1].isdigit() and t[:1] == "6"]
    if not starts:
        top = _guess_data_top_by_repetition(spans)
        if top is None:
            return None, None, None
        starts = [top]

    top = min(starts)
    seps = [y for y, x, t in spans if y > top and is_rule(t) and len(t) > 50]
    bottom = min(seps) - 3 if seps else max(y for y, _, _ in spans)
    return top, bottom, spans


def detect_columns(spans, top, bottom, dump):
    """
    三層式欄位左界偵測。
    第 1 層：表頭虛線的 x 座標（最可靠，本 PDF 適用）
    第 2 層：表頭文字的 x 座標
    第 3 層：資料 x 座標分群 + 已知欄位順序（會發出警告）
    """
    header = [(y, x, t) for y, x, t in spans if y < top]

    lefts = sorted({x for y, x, t in header if is_rule(t)})
    source = "L1 表頭虛線"
    if not lefts:
        lefts = sorted({x for y, x, t in header if len(t) <= 12 and y > top - 60})
        source = "L2 表頭文字"
    if not lefts:
        body_x = sorted({x for y, x, t in spans
                         if top <= y < bottom and not is_rule(t)})
        lefts = []
        for x in body_x:
            if not lefts or x - lefts[-1] > 20:
                lefts.append(x)
        lefts = lefts[:len(DOCUMENT_COLUMN_ORDER)]
        source = "L3 資料分群（欄位語意不可靠）"
        log("[warn] 無法由表頭偵測欄位，退回第 3 層資料 x 分群。"
            "欄位對應改用已知欄位順序，結果需人工確認。")

    if dump:
        log("  欄位偵測來源: %s" % source)
        log("  欄位左界: %s" % lefts)
    return lefts, source


def column_index(lefts, x):
    """
    將一個 span 的 x 座標歸屬到欄位。
    使用「小於等於該 x 的最大左界」而非最近距離，因數字欄靠右對齊、
    同一欄的 x 會隨位數漂移，用最近距離會把大數字誤判到下一欄。
    """
    idx = 0
    for i, left in enumerate(lefts):
        if x >= left:
            idx = i
        else:
            break
    return idx


def cluster_rows(spans, top, bottom, tolerance=ROW_Y_TOLERANCE):
    """依 y 座標把 span 分組成列；回傳 [[(x, text), ...], ...]"""
    body = [(y, x, t) for y, x, t in spans
            if top - 5 <= y < bottom and not is_rule(t)]
    rows = []
    for y, x, t in sorted(body):
        if rows and abs(rows[-1][0] - y) <= tolerance:
            rows[-1][1].append((x, t))
        else:
            rows.append([y, [(x, t)]])
    return [cells for _, cells in rows]


def header_field_candidates(text):
    """
    從一個表頭 span 找出所有可能的欄位別名（依代號長度由長到短）。
    必要時因應 PDF 把多個表頭合併成同一個 span 的情況，例如
    '訂購單號料 號' 同時包含 ORD_NO 與 MATM_NAME 兩個別名。
    """
    key = normalize_key(text)
    if not key:
        return []
    found = []
    for field, aliases in HEADER_ALIASES.items():
        best = None
        for alias in aliases:
            akey = normalize_key(alias)
            if akey and akey in key and (best is None or len(akey) > len(best)):
                best = akey
        if best:
            found.append((len(best), field))
    found.sort(reverse=True)
    return [field for _, field in found]


def infer_fields(lefts, header_spans, top, n_columns):
    """
    決定每個欄位對應的資料庫欄位名。
    以表頭文字比對為主；若表頭文字不足，才退回已知欄位順序補齊。

    合併 span 的處理：本 PDF 的「訂購單號」與「料號」被排在同一個 span（x=21），
    該 span 對應欄位 0，但含有 ORD_NO 與 MATM_NAME 兩個別名。距離最近者優先佔用
    欄位 0，落選的別名則依序填入其右側相鄰的未配對欄位（此處為欄位 1）。

    已知限制（實測，見 make_layout_pdfs.py 產生的版式測試檔）：
      - 整張版面左右位移可正確處理（欄位 x 改變不影響）。
      - 欄位順序改變**無法可靠處理**。PDF 引擎會把相鄰欄位的表頭
        合併成同一個 span（實測產生 '子公司  單     價'、
        '交貨日期  訂購單號'），此時無法從 span 還原哪個別名屬於哪一欄，
        資訊已不可逆遺失。
      - 偵測到順序與 DOCUMENT_COLUMN_ORDER 不符時，寧可讓欄位留空
        也不猜測，避免靜默產生欄位錯位的資料。
    """
    header_texts = [(x, t) for y, x, t in header_spans
                    if y < top and not is_rule(t)]

    # 收集候選：每個表頭別名對應「距離最近的欄位左界」
    # 注意要取最近的而非第一個落在容差內的，否則 '材料名稱/顏色代碼'(x=102)
    # 會被配到 lefts[1]=66 而非正確的 lefts[2]=96。
    candidates = []
    for x, text in header_texts:
        for field in header_field_candidates(text):
            if not lefts:
                continue
            nearest = min(range(len(lefts)), key=lambda i: abs(lefts[i] - x))
            dist = abs(lefts[nearest] - x)
            if dist < HEADER_X_TOLERANCE:
                candidates.append((nearest, dist, field))

    matched = [None] * n_columns
    used_fields = set()

    # 依「距離最近」優先配對，讓每欄盡量只被一個候選命中
    for i, _dist, field in sorted(candidates, key=lambda c: (c[1], c[0])):
        if field in used_fields:
            continue
        if matched[i] is None:
            matched[i] = field
            used_fields.add(field)

    # 合併 span 落選的別名：填入其右側最近的未配對欄位。
    # 例：'訂購單號料 號'(x=21) 同時含 ORD_NO 與 MATM_NAME，
    # ORD_NO 佔用欄位 0 後，落選的 MATM_NAME 應填入欄位 1。
    #
    # 此補位只在版面欄位順序與 DOCUMENT_COLUMN_ORDER 一致時才安全。
    # 欄位順序若改變，PDF 引擎仍可能把相鄰欄位合併成同一個 span，
    # 此時「往右填」會把別名塞進錯欄，因此改為不做補位並明確警告。
    order_ok = (_order_matches_document(matched)
                and not _merged_span_conflicts(candidates, n_columns))
    if order_ok:
        for i, _dist, field in sorted(candidates, key=lambda c: (c[0], c[1])):
            if field in used_fields:
                continue
            for j in range(i, n_columns):
                if matched[j] is None:
                    matched[j] = field
                    used_fields.add(field)
                    break
    else:
        unmatched_alias = [f for _i, _d, f in candidates if f not in used_fields]
        if unmatched_alias:
            log("[warn] 表頭合併 span 含有未能歸位的欄位別名（%s），"
                "且已偵測到欄位順序與預設不同，為避免欄位錯位不做補位。"
                % ", ".join(sorted(set(unmatched_alias))))

    # 仍無法判斷的欄位
    #
    # 只有在欄位順序確定為本文件預設順序時，才能用已知順序補齊。
    # 若表頭已判斷出至少一個欄位且其位置偏離預設順序，代表版面順序
    # 與 DOCUMENT_COLUMN_ORDER 不同，此時套用固定順序會產生靜默錯位，
    # 因此寧可留空也不要猜。
    if any(f is None for f in matched):
        if not order_ok:
            log("[warn] 部分欄位無法由表頭文字判斷，且已偵測到欄位順序"
                "與預設不同。為避免欄位錯位，這些欄位留空不猜測，"
                "請人工確認後再處理。")
            return matched
        log("[warn] 部分欄位無法由表頭文字判斷，改以已知欄位順序補齊。")
        missing = [f for f in DOCUMENT_COLUMN_ORDER]
        for cur in matched:
            if cur is not None:
                missing.remove(cur)
        pool = missing
        for i, cur in enumerate(matched):
            if cur is None and pool:
                matched[i] = pool.pop(0)

    return matched


def _merged_span_conflicts(candidates, n_columns):
    """
    判斷「合併 span 往右補位」是否安全。

    PDF 引擎會把相鄰兩欄的表頭併成同一個 span，例如
    '交貨日期  訂購單號'。此時無法從 span 判斷哪個別名屬於哪一欄，
    只能依序填入右側的未配對欄位 —— 這只在版面欄位順序與
    DOCUMENT_COLUMN_ORDER 完全一致時才正確。

    驗證方式：完整模擬一次補位，再檢查結果的欄位順序是否符合預設。
    只要補位後順序不符，就代表版面順序已改變，此時補位會填錯欄。

    candidates: [(欄位索引, 距離, 欄位名), ...]
    """
    assigned = {}
    for i, _dist, field in sorted(candidates, key=lambda c: (c[1], c[0])):
        assigned.setdefault(i, field)
    used = set(assigned.values())

    for i, _dist, field in sorted(candidates, key=lambda c: (c[0], c[1])):
        if field in used:
            continue
        target = next((j for j in range(i, n_columns) if j not in assigned), None)
        if target is None:
            continue
        assigned[target] = field
        used.add(field)

    ordered = [assigned.get(i) for i in range(n_columns)]
    return not _order_matches_document(ordered)


def _order_matches_document(matched):
    """
    判斷已判斷出的欄位是否符合 DOCUMENT_COLUMN_ORDER 的相對順序。

    只要有任何一個已判斷欄位出現在比預設更靠左的位置，
    就視為版面順序與預設不同，固定順序補齊將不安全。
    """
    known = {f: i for i, f in enumerate(DOCUMENT_COLUMN_ORDER) if f is not None}
    seen = [(i, known[f]) for i, f in enumerate(matched) if f in known]
    for (i1, k1), (i2, k2) in zip(seen, seen[1:]):
        if k1 >= k2:
            return False
    return True


def extract(pdf_path, dump=False):
    """解析整份 PDF，回傳 (FENTAY 記錄陣列, 診斷資訊)。"""
    doc = fitz.open(pdf_path)
    records = []
    diagnostics = {"pages": [], "dropped": set()}
    try:
        for page_index in range(doc.page_count):
            spans = collect_spans(doc[page_index])
            top, bottom, _ = find_table_bounds(spans)
            if top is None:
                diagnostics["pages"].append(
                    {"page": page_index + 1, "rows": 0, "note": "找不到訂單資料"})
                log("[error] 第 %d 頁找不到訂單資料區，可能是掃描件（無文字層）"
                    % (page_index + 1))
                continue

            lefts, source = detect_columns(spans, top, bottom, dump)
            fields = infer_fields(lefts, spans, top, len(lefts))
            rows = cluster_rows(spans, top, bottom)

            page_records = []
            for cells in rows:
                raw = {}
                for x, text in cells:
                    raw.setdefault(column_index(lefts, x), []).append(text)
                row = {}
                for idx, parts in raw.items():
                    field = fields[idx] if idx < len(fields) else None
                    if field:
                        row[field] = "".join(parts)
                # 以實際欄位名判定資料列，不假設 ORD_NO 一定在欄位 0
                if "ORD_NO" in row:
                    page_records.append(row)

            for row in page_records:
                # row 的鍵已由 infer_fields 判定為欄位名，
                # fentay_common.apply_mapping 會自動辨識並套用同一份規則
                records.append(apply_mapping(row))

            diagnostics["pages"].append({
                "page": page_index + 1, "rows": len(page_records),
                "columns": len(lefts), "source": source,
                "fields": fields,
            })
            if dump:
                log("  第 %d 頁: %d 筆, %d 欄" % (page_index + 1, len(page_records), len(lefts)))
                log("    欄位對應: %s" % list(zip(lefts, fields)))
    finally:
        doc.close()
    return records, diagnostics


def collect_amounts(pdf_path):
    """
    單獨擷取每列的「金額」欄原始值，供獨立交叉驗證使用。
    金額欄依 DOC_AMOUNT_INDEX 指定的位置取得（不經欄位名稱比對，
    因為 MAPPING.md 定義金額欄不匯入資料庫，沒有對應的欄位名）。
    """
    doc = fitz.open(pdf_path)
    amounts = {}
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            spans = collect_spans(page)
            top, bottom, _ = find_table_bounds(spans)
            if top is None:
                continue
            lefts, _source = detect_columns(spans, top, bottom, False)
            if DOC_AMOUNT_INDEX >= len(lefts):
                continue
            fields = infer_fields(lefts, spans, top, len(lefts))
            # 以實際欄位名定位，而非假設欄位 0 就是訂購單號
            if "ORD_NO" not in fields:
                continue
            ord_no_idx = fields.index("ORD_NO")
            for cells in cluster_rows(spans, top, bottom):
                ord_no = None
                amount = None
                for x, text in cells:
                    idx = column_index(lefts, x)
                    if idx == ord_no_idx:
                        ord_no = text
                    elif idx == DOC_AMOUNT_INDEX:
                        amount = text
                if ord_no:
                    amounts[ord_no] = amount
    finally:
        doc.close()
    return amounts


def cross_validate(records, amounts, tolerance=1.0):
    """
    獨立驗證：用 QTY x PRICE 是否等於 PDF 的「金額」欄。
    此檢查完全不依賴 expected.json，因此即使 expected.json 是由本程式產生的，
    仍可作為獨立證據。若欄位歸屬有誤，QTY 與 PRICE 會對到不同資料列，
    相乘結果便不會等於該列的金額，因此此檢查可有效偵測欄位錯位。
    """
    verified = 0
    mismatches = []
    for record in records:
        ord_no = record.get("ORD_NO")
        qty = to_number(record.get("QTY"), 2)
        price = to_number(record.get("PRICE"), 3)
        amount = to_number(amounts.get(ord_no), 2)
        if qty is None or price is None or amount is None:
            continue
        verified += 1
        calc = round(qty * price, 2)
        if abs(calc - amount) > tolerance:
            mismatches.append((ord_no, calc, amount))
    return verified, mismatches


def run_self_tests():
    """內建自我檢查，供測試腳本呼叫。回傳 [(bool, msg), ...]"""
    results = []

    results.append((column_index([21, 66, 96, 232, 288], 21) == 0, "x=21 -> 欄位 0"))
    results.append((column_index([21, 66, 96, 232, 288], 66) == 1, "x=66 -> 欄位 1"))
    results.append((column_index([21, 66, 96, 232, 288], 96) == 2, "x=96 -> 欄位 2"))
    results.append((column_index([21, 66, 96, 232, 288], 231) == 2, "x=231 -> 欄位 2"))
    results.append((column_index([21, 66, 96, 232, 288], 232) == 3, "x=232 -> 欄位 3"))
    results.append((column_index([21, 66, 96, 232, 288], 323) == 4,
                    "x=323 (靠右數字) -> 欄位 4，非最近距離 3"))
    results.append((column_index([21, 66, 96, 232, 288], 565) == 4,
                    "x=565 超出最後左界 -> 歸最後欄位"))

    Q = chr(34)
    spans = [
        (107, 21, "6AF1101"), (107, 66, "3056G"),
        (107, 96, "44" + Q + "74F" + Q), (107, 232, "44" + Q),
        (107, 313, "820.0"), (107, 350, "碼"), (107, 394, "73.00"),
        (108, 449, "59,860.00"), (107, 503, "2026/03/27"), (107, 565, "LU1"),
    ]
    rows = cluster_rows(spans, 107, 641)
    results.append((len(rows) == 1, "y 差 1pt 的 span 合併為同一列"))
    spans2 = spans + [(132, 21, "6AF1102"), (132, 66, "307LJ")]
    results.append((len(cluster_rows(spans2, 107, 641)) == 2, "相距 25pt 視為不同列"))

    results.append((is_rule("--------"), "'--------' 判定為虛線"))
    results.append((is_rule("44" + Q) is False, "44\" 不判定為虛線"))

    results.append((header_field_candidates("訂購單號料 號") == ["ORD_NO", "MATM_NAME"],
                    "合併 span '訂購單號料 號' 解析出兩個別名"))
    results.append((header_field_candidates("金      額") == [],
                    "'金      額' 不對應任何匯入欄位"))

    # 以欄位名為鍵的列，必須與以中文標題為鍵得到相同結果（共用同一份規則）
    record = apply_mapping({
        "ORD_NO": "6AF1101", "MATM_NAME": "3056G",
        "MATM_DESC": "44" + Q + "74F" + Q,
        "WIDE": "44" + Q, "QTY": "820.0", "UNIT": "碼",
        "PRICE": "73.00", "NEED_DATE": "2026/03/27", "NEED_CUST": "LU1",
    })
    expected_record = {
        "ORD_NO": "6AF1101", "MATM_NAME": "3056G",
        "MATM_DESC": "44" + Q + "74F" + Q, "WIDE": "44",
        "QTY": 820.0, "UNIT": "YD", "PRICE": 73.0,
        "NEED_DATE": "20260327", "NEED_CUST": "LU1",
        "BRAND_NO": "", "CUST_NO": "", "CONTACT_NO": "",
    }
    results.append((record == expected_record,
                    "apply_mapping 以欄位名為鍵 完整轉換正確"))
    results.append((Q not in record["WIDE"], "WIDE 已去除雙引號"))
    results.append((Q in record["MATM_DESC"], "MATM_DESC 保留雙引號"))

    # 交叉驗證：欄位錯位時必須被偵測到
    ok_verify, bad_verify = cross_validate(
        [{"ORD_NO": "A", "QTY": 820.0, "PRICE": 73.0}], {"A": "59,860.00"})
    results.append((ok_verify == 1 and not bad_verify, "QTY x PRICE 正確時通過"))
    ok2, bad2 = cross_validate(
        [{"ORD_NO": "A", "QTY": 2.0, "PRICE": 73.0}], {"A": "59,860.00"})
    results.append((ok2 == 1 and len(bad2) == 1, "QTY 對到別列時被偵測為不一致"))

    return results


def main():
    parser = argparse.ArgumentParser(
        description="方案 C：直接解析 PDF 文字層為 FENTAY_B2B JSON")
    parser.add_argument("--pdf", required=True, help="PDF 路徑")
    parser.add_argument("--out", default=None,
                        help="輸出 JSON 路徑，預設 output/result.C.json")
    parser.add_argument("--out-dir", default=None,
                        help="輸出目錄，預設為專案下的 output/")
    parser.add_argument("--compare", default=None, help="基準答案 JSON 路徑")
    parser.add_argument("--report", default=None, help="報告路徑，預設與 out 同名加時間戳")
    parser.add_argument("--show-diffs", type=int, default=10,
                        help="console 顯示的差異筆數，-1 代表全部（報告檔一律完整）")
    parser.add_argument("--dump-columns", action="store_true", help="輸出欄位偵測診斷")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        sys.exit("找不到 PDF: %s" % args.pdf)

    out_dir = resolve_out_dir(args.out_dir)
    args.out = args.out or os.path.join(out_dir, "result.C.json")
    slug = timestamp_slug()

    started_at = now_iso()
    started = time.time()
    log("解析 PDF: %s" % os.path.basename(args.pdf))
    result = {"success": True, "recordCount": 0, "data": []}
    diagnostics = {"pages": [], "dropped": set()}
    try:
        data, diagnostics = extract(args.pdf, args.dump_columns)
        result = {"success": True, "recordCount": len(data), "data": data}
    except Exception as exc:
        log("[error] 解析失敗: %s" % exc)
    elapsed = time.time() - started

    # OCR 失真修正。文字層不會產生此類失真，實測為 no-op；
    # 仍統一套用以維持兩條路線共用同一份規則。
    result["data"], ocr_fixed = apply_ocr_fixes(result.get("data", []))
    if ocr_fixed:
        log("OCR 失真修正: %d 筆" % ocr_fixed)

    build_output(
        result,
        source={
            "route": "C",
            "mode": "text-layer",
            "model": None,
            "pdf": os.path.basename(args.pdf),
            "pages": len(diagnostics["pages"]),
            "ocrColorCodeFixed": ocr_fixed,
        },
        elapsed_sec=elapsed,
        started_at=started_at,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    problems = validate(result)
    report = [
        "產出時間: %s" % result["_meta"]["generatedAt"],
        "開始時間: %s" % started_at,
        "耗時: %.3f 秒" % elapsed,
        "來源: 路線 C (PDF 文字層直接解析，未使用 LLM)",
        "輸入 PDF: %s" % os.path.basename(args.pdf),
        "OCR 色碼修正: %d 筆（文字層解析，預期為 0）" % ocr_fixed,
        "輸出 JSON: %s" % args.out,
        "  (JSON 為固定檔名，每次執行覆蓋；本報告檔名含時間戳以保留紀錄)",
        "recordCount: %d / data 長度: %d" % (result["recordCount"], len(result["data"])),
        "",
        "=== 分頁診斷 ===",
    ]
    for page in diagnostics["pages"]:
        report.append("  第 %d 頁: %d 筆, %d 欄 (%s)"
                      % (page["page"], page["rows"], page.get("columns", 0),
                         page.get("source", "-")))
        if "note" in page:
            report.append("    %s" % page["note"])
    if diagnostics["dropped"]:
        report.append("已略過不匯入欄位: %s" % ", ".join(sorted(diagnostics["dropped"])))

    report.extend(["", "驗證問題 (%d):" % len(problems)])
    report.extend("  - " + p for p in problems[:20])
    if len(problems) > 20:
        report.append("  ... 另有 %d 個驗證問題" % (len(problems) - 20))

    # 獨立交叉驗證：重新從 PDF 取「金額」欄，不依賴 expected.json
    try:
        amounts = collect_amounts(args.pdf)
        verified, mismatches = cross_validate(result["data"], amounts)
        report.extend([
            "",
            "=== 獨立交叉驗證 (QTY x PRICE vs 金額) ===",
            "此檢查不依賴 expected.json，為本程式正確性的獨立證據。",
            "若欄位歸屬錯位，QTY 與 PRICE 會對到不同資料列，相乘即不會等於該列金額。",
            "驗證筆數: %d / %d" % (verified, len(result["data"])),
            "不一致: %d" % len(mismatches),
        ])
        for ord_no, calc, amount in mismatches[:10]:
            report.append("  %s 計算=%s PDF金額=%s" % (ord_no, calc, amount))
    except Exception as exc:
        report.append("交叉驗證執行失敗: %s" % exc)

    if args.compare:
        if not os.path.isfile(args.compare):
            sys.exit("找不到基準答案: %s" % args.compare)
        with open(args.compare, "r", encoding="utf-8") as f:
            expected = json.load(f)
        report.extend([
            "",
            "=== 與基準答案比對 ===",
            "注意: expected.json 本身即由 PDF 文字層產生，",
            "      因此此處的高正確率屬循環比對，不能作為獨立驗證。",
            "      真正的獨立證據請看上面的 QTY x PRICE 交叉驗證。",
            "",
            # 報告檔寫入完整差異，便於事後診斷每一處錯誤
            compare(expected, result, show=args.show_diffs, full=True),
        ])

    report_text = "\n".join(report)
    # 報告檔名帶時間戳以保留每次執行紀錄；
    # JSON 使用固定檔名（每次覆蓋）。可用 --report 指定自訂路徑。
    default_report = os.path.splitext(args.out)[0] + ".%s.report.txt" % slug
    report_path = args.report or default_report
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    log("")
    log("完整報告: %s" % report_path)
    log("輸出目錄: %s" % out_dir)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
