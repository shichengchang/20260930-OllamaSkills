#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_layout_pdfs.py - 產生欄位順序可控的測試 PDF。

用途
----
`pdf_text_fentay.py` 的 `find_table_bounds()` 內含兩個硬編碼假設：
    1. 資料區第一欄的 x 座標必定小於 30（常數 `30`）
    2. 資料區第一欄的內容必定以 "6" 開頭（疑似針對訂購單號格式）

本腳本產生數個版式不同的 PDF，讓呼叫端實際跑過完整的
`collect_spans` -> `find_table_bounds` -> `detect_columns`
-> `infer_fields` 管線，藉此確認上述假設被打破時的實際行為。

本腳本只產生檔案，不修改 `pdf_text_fentay.py`。
"""

import os
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("缺少 PyMuPDF，請執行: pip install PyMuPDF")

# 真實 PDF 的欄位定義：(表頭文字, 欄位名, 建議 x 座標, 建議資料 x 座標, 建議寬度)
# 寬度用於計算表頭虛線長度與資料欄位間距。
COLUMN_SPECS = [
    ("訂購單號", "ORD_NO", 21, 21, 40),
    ("料 號", "MATM_NAME", 66, 66, 26),
    ("材料名稱/顏色代碼", "MATM_DESC", 96, 96, 130),
    ("規    格", "WIDE", 232, 232, 50),
    ("數     量", "QTY", 288, 313, 55),
    ("單位", "UNIT", 349, 350, 20),
    ("單     價", "PRICE", 373, 394, 50),
    ("金      額", "AMOUNT", 429, 449, 70),
    ("交貨日期", "NEED_DATE", 503, 503, 70),
    ("子公司", "NEED_CUST", 556, 565, 30),
]

# 每一欄三列樣本資料，刻意混合數字格式以貼近真實 PDF
SAMPLE_ROWS = [
    ["6AF1101", "3056G", '44"74F黃LJ-A8-P網布/(74F)', '44"', "820.0", "碼", "73.00",
     "59,860.00", "2026/03/27", "LU1"],
    ["6AF1102", "307LJ", '44"0AH灰LJ-B4網布/(0AH)', '44"', "54.0", "碼", "29.00",
     "1,566.00", "2026/03/27", "LU1"],
    ["6AF1103", "309QS", '40"10A LJA8P網水性漿/(10A)', '40"', "2.0", "碼", "104.00",
     "208.00", "2026/04/10", "DS1"],
    ["6AF1104", "311LT", '60"12B 黑PE網/(12B)', '60"', "1,460.0", "碼", "8.50",
     "12,410.00", "2026/04/15", "DS1"],
    ["6AF1105", "318HN", '38"9K 白網布/(9K)', '38"', "31.0", "碼", "126.00",
     "3,906.00", "2026/04/18", "LU1"],
]

# 以下 y 座標取自真實豐泰.pdf 的實際結構：
#   y=87~89 表頭文字、y=94~95 表頭虛線、y=107 第一筆資料
HEADER_TEXT_Y = 88      # 表頭文字 y（必須緊鄰虛線上方）
DASH_Y = 94             # 表頭虛線 y
FIRST_ROW_Y = 107       # 第一筆資料 y
ROW_PITCH = 25          # 資料列間距
# 頁面寬度預留右側空白，避免欄位右移後超出頁面而被裁掉
PAGE_W, PAGE_H = 760, 842

HEADER_HEIGHT = 4
FOOTER_RULE_LEN = 200

# PyMuPDF 內建中文字型代號（"china-t" 為繁體），確保中文可正常嵌入
CJK_FONTNAME = "china-t"

# 抬頭區，取自真實豐泰.pdf 的實際內容與座標。
# 這些文字位於表頭上方且 x 靠左，是「哪一欄最靠左」判定的干擾來源。
PREAMBLE_SPANS = [
    (17, 264, "訂 購 單"),
    (38, 21, "致  供應商:"),
    (38, 82, "隆芳興業股份有限公司"),
    (38, 477, "列印編號:"),
    (39, 528, "001714535"),
    (51, 42, "地  址:"),
    (51, 81, "(511)彰化縣社頭鄉平和村水井巷5-2號"),
    (51, 477, "訂購日期:"),
    (51, 528, "2026/03/20"),
    (63, 477, "幣    別: 新台幣"),
]


def assign_slots(order, x_offset=0):
    """
    依新的欄位順序重新分配 x 座標槽位。

    關鍵：欄位順序改變時，欄位本身必須搬到新的 x 位置，
    否則產生的 PDF 順序根本沒變，測不到任何東西。
    這裡以原始欄位的 x 座標為「槽位」，把 order[k] 這個欄位
    放到第 k 個槽位上。
    """
    slots = []
    for k, idx in enumerate(order):
        base_header_x = COLUMN_SPECS[k][2]
        base_data_x = COLUMN_SPECS[k][3]
        width = COLUMN_SPECS[idx][4]
        slots.append((base_header_x, base_data_x, width))
    return [(hx + x_offset, dx + x_offset, w) for hx, dx, w in slots]


def build_rows(order, x_offset=0):
    """依欄位順序與槽位配置產生每一列的 (x, text)。"""
    slots = assign_slots(order, x_offset)
    rows = []
    for r in SAMPLE_ROWS:
        cells = []
        for k, idx in enumerate(order):
            _hx, dx, _w = slots[k]
            cells.append((dx, r[idx]))   # (x, text)
        rows.append(sorted(cells))
    return rows


def build_spans(order, x_offset=0, n_rows=3):
    """
    產生 [(y, x, text)] 形式的 span 清單，模擬 collect_spans 的輸出。

    order : 欄位索引的排列，代表實際的欄位順序（左至右）
    x_offset : 所有 x 座標的位移，用來測試 x < 30 的硬編碼假設
    n_rows : 資料列數
    """
    spans = []
    slots = assign_slots(order, x_offset)

    # 抬頭區（真實 PDF 在表頭上方有供應商/地址/日期/幣別等資訊）。
    # 這些文字同樣會被 collect_spans 收進來，是「最左欄」判定的干擾來源，
    # 必須一併產生才能測出貼近真實的行為。
    for y, x, text in PREAMBLE_SPANS:
        spans.append((y, x + x_offset, text))

    # 表頭文字與表頭虛線
    for k, idx in enumerate(order):
        title = COLUMN_SPECS[idx][0]
        hx = slots[k][0]
        spans.append((HEADER_TEXT_Y, hx, title))
    for k, idx in enumerate(order):
        hx, _dx, width = slots[k]
        # 虛線長度需小於欄寬，否則會與右側欄位的虛線被 PDF 引擎併成同一 span。
        # 真實豐泰.pdf 的表頭虛線長度約為欄寬的一半，這裡依循同樣比例。
        dash_len = max(4, (width - 6) // 2)
        spans.append((DASH_Y, hx, "-" * dash_len))

    # 資料列
    rows = build_rows(order, x_offset)
    for r in range(min(n_rows, len(rows))):
        y = FIRST_ROW_Y + r * ROW_PITCH
        for (x, text) in rows[r]:
            spans.append((y, x, text))

    # 頁尾長虛線分隔線，供 find_table_bounds 判定下界
    spans.append((PAGE_H - 40, 30, "-" * FOOTER_RULE_LEN))

    spans.sort(key=lambda s: (s[0], s[1]))
    return spans


def write_pdf(path, spans, pages=1):
    """把 span 清單畫成一頁（或多頁）沒有框線的表格 PDF。"""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        # 必須明確指定 CJK 字型，預設 Helvetica 不含中文字形會變成空白
        page.insert_text((0, 0), "", fontsize=1, fontname=CJK_FONTNAME)
        for y, x, text in spans:
            # 尾端補三個空格：欄距不足時 PDF 引擎會把相鄰文字併入同一個 span，
            # 真實豐泰.pdf 的表頭是各欄獨立的 span（'規    格'、'數     量' 分開），
            # 不補空格會重現出比真實檔案更惡化的合併，測不到真正的行為。
            page.insert_text((x, y + 10), text + "   ", fontsize=9, fontname=CJK_FONTNAME)
    doc.save(path)
    doc.close()
    return path


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "layout_tests"
    os.makedirs(out_dir, exist_ok=True)

    base_order = list(range(len(COLUMN_SPECS)))
    qty_first = [4, 0, 1, 3, 5, 6, 8, 9, 7, 2]
    shuffled = [9, 6, 2, 4, 7, 5, 8, 0, 3, 1]

    cases = [
        ("01_baseline.pdf", "原始順序，x=21 起", base_order, 0),
        ("02_qty_first.pdf", "QTY 移到最前，x=21 起", qty_first, 0),
        ("03_shuffled.pdf", "欄位完全打亂，x=21 起", shuffled, 0),
        ("04_offset_x30.pdf", "原始順序，整體右移 9pt（第一欄 x=30）", base_order, 9),
        ("05_offset_x50.pdf", "原始順序，整體右移 29pt（第一欄 x=50）", base_order, 29),
    ]

    written = []
    for name, desc, order, offset in cases:
        path = os.path.join(out_dir, name)
        spans = build_spans(order, offset, n_rows=3)
        write_pdf(path, spans, pages=1)
        written.append((name, desc, order, offset))
        print("已產生 %-22s %s" % (name, desc))

    print()
    print("欄位順序對照:")
    for name, desc, order, offset in written:
        names = " > ".join(COLUMN_SPECS[i][1] for i in order)
        first_x = COLUMN_SPECS[order[0]][2] + offset
        print("  %-22s 首欄 x=%-4d %s" % (name, first_x, names))

    print()
    print("目錄: %s" % os.path.abspath(out_dir))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()