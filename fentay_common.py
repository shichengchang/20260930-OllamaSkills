#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fentay_common.py - FENTAY_B2B 共用層：MAPPING.md 規則實作與輸出契約。

此模組不依賴任何 PDF 或 LLM 套件，只依賴標準函式庫，因此可同時被兩條解析路線共用：

    方案 B  ollama_fentay.py   PDF -> PNG -> vision 模型 -> 本模組規則 -> JSON
    方案 C  pdf_text_fentay.py PDF 文字層 -> 座標分組 -> 本模組規則 -> JSON

抽取出本模組的目的，是讓「MAPPING.md 的轉換規則」只有一份實作，
兩條路線的比對結果才具有可比性，也避免規則被誤認為某一條路線的私有邏輯。

本模組包含三類內容：
  1. 欄位定義與轉換規則（apply_mapping 及其輔助函式）
  2. 標題文字比對（實作 MAPPING.md「以標題文字比對為主，與欄位順序無關」）
  3. 輸出契約檢查（validate 結構、compare 內容）

不屬於本模組者：
  - PDF 渲染與座標解析        -> 各自屬於對應路線
  - LLM 回應的 JSON 容錯解析   -> ollama_fentay.py 專用
  - prompt 組裝與 Ollama API    -> ollama_fentay.py 專用
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

# 預設輸出資料夾，所有解析結果集中於此，避免散落在專案根目錄
DEFAULT_OUT_DIR = "output"

# PDF 轉圖時的預設解析度。
#
# 選 400 的理由：vision 模型在 8pt 中文字時，200 DPI 僅約 22px 字高，
# 數字 0 與字母 O 的字形幾乎無法區分。400 DPI 約 44px 字高即可分辨。
#
# 上限依模型而異，請勿盲目提高：
#   qwen3.5 視覺面積上限 16,777,216 px²，400 DPI 約 11.27M，安全；450 DPI 為極限
#   gemma4 為固定解析度架構，提高 DPI 無實益
# 視覺 token 數約為 像素數 / 1024，200 DPI 約 2,753；400 DPI 約 11,007。
DEFAULT_DPI = 400

# 台灣時區（UTC+8）。PDF 訂單為台灣廠商，使用當地時間較符合閱讀習慣。
TAIWAN_TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 欄位定義

FIELDS = ["ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "QTY", "UNIT", "PRICE",
          "NEED_DATE", "NEED_CUST", "BRAND_NO", "CUST_NO", "CONTACT_NO"]

# 依 MAPPING.md「欄位預設值規則」，這三欄固定填空字串
FIXED_EMPTY_FIELDS = ("BRAND_NO", "CUST_NO", "CONTACT_NO")

# 依 MAPPING.md「欄位類型對照」，缺漏時填 null 的數值欄位
NULLABLE_FIELDS = ("QTY", "PRICE")

# MAPPING.md：UNIT 轉換表
UNIT_MAP = {
    "碼": "YD", "码": "YD", "YD": "YD",
    "Y-2": "Y2", "Y2": "Y2",
    "公尺": "M", "公尺 ": "M", "米": "M", "M": "M",
    "公斤": "KG", "KG": "KG",
    "個": "PC", "个": "PC", "PC": "PC",
}

# MAPPING.md：標題別名（任一符合即可）。以標題文字比對為主，與欄位順序無關。
HEADER_ALIASES = {
    "ORD_NO": ["訂購單號", "訂單號碼", "訂單號", "PO Number", "PO NO"],
    "MATM_NAME": ["材料編號", "Item No", "Part No", "料號", "品號"],
    "MATM_DESC": ["材料名稱/顏色代碼", "材料名稱／顏色代碼", "顏色代碼",
                  "Material Name", "Description", "材料名稱", "品名"],
    "WIDE": ["幅寬", "規格", "Width", "Spec"],
    "QTY": ["數量", "訂購量", "Quantity", "Qty"],
    "UNIT": ["單位", "Unit"],
    "PRICE": ["單價", "Unit Price", "Price"],
    "NEED_DATE": ["交貨日期", "需求日期", "出貨日", "Delivery Date", "Ship Date"],
    "NEED_CUST": ["需求子公司", "需求公司", "子公司", "Sub Company"],
}

# 這些標題不匯入資料庫，直接略過
HEADER_IGNORED = ["金額", "小計", "合計", "Amount", "Total"]


def log(msg):
    """進度訊息一律寫 stderr，避免污染 stdout 的 JSON 輸出。"""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 輸出與時間

def now_iso():
    """回傳台灣時區的 ISO 8601 時間字串，例如 2026-09-30T14:35:12+08:00。"""
    return datetime.now(TAIWAN_TZ).isoformat(timespec="seconds")


def timestamp_slug():
    """回傳適合檔名的時間戳，例如 20260930-143512。"""
    return datetime.now(TAIWAN_TZ).strftime("%Y%m%d-%H%M%S")


def resolve_out_dir(out_dir=None):
    """
    決定輸出目錄，預設為專案下的 output/，不存在則建立。
    回傳絕對路徑。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(base, out_dir or DEFAULT_OUT_DIR)
    os.makedirs(target, exist_ok=True)
    return target


def build_output(result, source, elapsed_sec=None, started_at=None):
    """
    在結果物件加上 _meta 區塊，記錄來源與時間。

    放在 _meta 而非頂層，是為了不影響既有的
    {success, recordCount, data} 結構，呼叫端仍可直接取用 data。
    """
    meta = {
        "generatedAt": now_iso(),
        "source": source,
    }
    if started_at:
        meta["startedAt"] = started_at
    if elapsed_sec is not None:
        meta["elapsedSec"] = round(elapsed_sec, 3)
    result["_meta"] = meta
    return result


# ---------------------------------------------------------------- 標題比對

def normalize_key(text):
    """把標題文字正規化：全形轉半形、移除所有空白與常見分隔符。"""
    if not isinstance(text, str):
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            continue
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        if not ch.isspace() and ch not in "/\\|-_":
            out.append(ch)
    return "".join(out).upper()


def map_header_to_field(header):
    """以標題文字比對決定資料庫欄位；回傳 (欄位名 或 None, 是否為忽略欄位)。"""
    key = normalize_key(header)
    if not key:
        return None, False
    for alias in HEADER_IGNORED:
        if normalize_key(alias) in key:
            return None, True
    best_field, best_len = None, 0
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            akey = normalize_key(alias)
            if akey and akey in key and len(akey) > best_len:
                best_field, best_len = field, len(akey)
    return best_field, False


def resolve_field(key):
    """
    解析一列資料的鍵，回傳 (欄位名 或 None, 是否為忽略欄位)。

    兩條解析路線餵入的鍵格式不同，因此在此統一處理：
      - 方案 B（hybrid）：模型輸出原始中文標題，需做標題文字比對
      - 方案 C           : 座標分組時已由表頭判定欄位名，鍵即欄位名
    先檢查是否已是合法欄位名，是則直接採用；否則才做標題比對。
    """
    if isinstance(key, str) and key in FIELDS:
        return key, False
    return map_header_to_field(key)


# ---------------------------------------------------------------- 值轉換

def to_date_yyyymmdd(raw):
    """依 MAPPING.md 日期規則轉換；無法辨識回傳空字串。"""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    text = text.replace(".", "/").replace(" ", "")
    if re.fullmatch(r"\d{8}", text):
        return text
    m = re.fullmatch(r"(\d{3,4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 1911:  # 民國年
            year += 1911
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return ""
        return "%04d%02d%02d" % (year, month, day)
    return ""


def to_number(raw, digits):
    """去除千分位逗號後取小數；無法解析回傳 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw), digits)
    text = str(raw).replace(",", "").replace("，", "").replace(" ", "").strip()
    text = re.sub(r"^[^\d\.\-]+", "", text)
    text = re.sub(r"[^\d\.]+$", "", text)
    if not text or text in {".", "-", "-."}:
        return None
    try:
        return round(float(text), digits)
    except ValueError:
        return None


def to_text(raw):
    """轉為字串；None 轉空字串，數值去掉尾端 .0。"""
    if raw is None:
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    return str(raw).strip()


def to_wide(raw):
    """WIDE 提取規則：去除雙引號等符號僅保留數字（44\" -> 44）。"""
    return re.sub(r"\D", "", to_text(raw))


def empty_record():
    """建立一筆符合 FENTAY_B2B 型態的空記錄。"""
    record = {field: "" for field in FIELDS}
    for field in NULLABLE_FIELDS:
        record[field] = None
    return record


def apply_mapping(row, dropped=None):
    """
    套用 MAPPING.md 的轉換規則，產生一筆 FENTAY_B2B 記錄。

    row 的鍵可以是 PDF 原始中文標題，也可以已是 FENTAY 欄位名，
    由 resolve_field 自動判斷，兩條解析路線共用同一份規則實作。

    dropped 若提供一個 set，會收集被判定為「不匯入」的標題名稱。
    """
    record = empty_record()

    for key, raw in row.items():
        field, ignored = resolve_field(key)
        if ignored:
            if dropped is not None:
                dropped.add(to_text(key))
            continue
        if field is None:
            continue

        value = raw[0] if isinstance(raw, list) else raw
        if field == "WIDE":
            record["WIDE"] = to_wide(value)
        elif field == "QTY":
            record["QTY"] = to_number(value, 2)
        elif field == "PRICE":
            record["PRICE"] = to_number(value, 3)
        elif field == "UNIT":
            unit_key = to_text(value).replace(" ", "").upper()
            record["UNIT"] = UNIT_MAP.get(unit_key, to_text(value))
        elif field == "NEED_DATE":
            record["NEED_DATE"] = to_date_yyyymmdd(value)
        else:
            record[field] = to_text(value)

    for field in FIXED_EMPTY_FIELDS:
        record[field] = ""
    return record


# ---------------------------------------------------------------- OCR 失真修正

# MATM_DESC 開頭的色碼，例如 0AVLJA4497K9EPM5回網/(0AV) 的「0AV」
_PREFIX_COLOR_CODE = re.compile(r"^([0O])([A-Z]{2})")
# MATM_DESC 尾端的括號色碼，例如同例的「(0AV)」
_PAREN_COLOR_CODE = re.compile(r"/\(([0O])([A-Z]{2})\)")


def fix_ocr_color_code(desc):
    """
    修正 MATM_DESC 開頭色碼的 O/0 混淆（vision OCR 失真）。

    豐泰訂單的材料名稱常以色碼開頭，且同一筆的尾端括號會重複該色碼，
    例如 0AVLJA4497K9EPM5回網/(0AV)。vision 模型在低解析度下會把
    數字 0 認成字母 O，造成前綴與括號內不一致。

    僅在下列條件同時成立時修正，其他情況一律不動：
      1. 前綴碼為 O 或 0 加兩個大寫字母
      2. 尾端括號內為 O 或 0 加兩個大寫字母
      3. 兩者後兩個字母完全相同
      4. 兩者首字元不同（一個 O、一個 0）
    以數字 0 為準修正。條件嚴格，因此對正常資料為 no-op。

    回傳 (修正後字串, 是否曾修正)。
    """
    if not isinstance(desc, str) or not desc:
        return desc, False

    prefix = _PREFIX_COLOR_CODE.match(desc)
    paren = _PAREN_COLOR_CODE.search(desc)
    if not prefix or not paren:
        return desc, False
    if prefix.group(2) != paren.group(2):
        return desc, False
    if prefix.group(1) == paren.group(1):
        return desc, False

    index = prefix.start(1)
    return desc[:index] + "0" + desc[index + 1:], True


def apply_ocr_fixes(records):
    """
    對整批記錄套用 OCR 失真修正，回傳 (記錄, 修正筆數)。

    路線 C 讀取 PDF 文字層，不會產生此類失真，實測為 no-op；
    仍統一在此套用，是為了讓兩條路線共用同一份修正規則而不致分歧。
    """
    fixed = 0
    for record in records:
        desc = record.get("MATM_DESC")
        corrected, changed = fix_ocr_color_code(desc)
        if changed:
            record["MATM_DESC"] = corrected
            fixed += 1
    return records, fixed


# ---------------------------------------------------------------- 輸出契約

def validate(result):
    """檢查輸出是否符合 FENTAY_B2B 結構，回傳問題清單（空清單代表通過）。"""
    problems = []
    if not isinstance(result, dict):
        return ["輸出不是 JSON 物件"]
    data = result.get("data")
    if not isinstance(data, list):
        return ["缺少 data 陣列"]
    if result.get("recordCount") != len(data):
        problems.append("recordCount=%s 但 data 長度=%d，不一致"
                        % (result.get("recordCount"), len(data)))
    if not data:
        problems.append("data 為空")
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            problems.append("第 %d 筆不是物件" % (i + 1))
            continue
        if not to_text(row.get("ORD_NO")):
            problems.append("第 %d 筆缺少 ORD_NO" % (i + 1))
        if row.get("QTY") is None:
            problems.append("第 %d 筆 QTY 為 null" % (i + 1))
        missing = [f for f in FIELDS if f not in row]
        if missing:
            problems.append("第 %d 筆缺少欄位 %s" % (i + 1, ",".join(missing)))
    return problems


def diff_rows(expected, actual):
    """
    以 ORD_NO 為鍵比對兩份結果的每個欄位。

    回傳 (命中筆數, 差異清單)，差異清單元素為 (ORD_NO, 欄位, 預期值, 實際值)。
    """
    exp_rows = {to_text(r.get("ORD_NO")): r for r in expected.get("data", [])}
    act_rows = {}
    for row in actual.get("data", []):
        act_rows[to_text(row.get("ORD_NO"))] = row

    common = sorted(set(exp_rows) & set(act_rows))
    diffs = []
    for ord_no in common:
        e, a = exp_rows[ord_no], act_rows[ord_no]
        for field in FIELDS:
            ev, av = e.get(field), a.get(field)
            if field in ("QTY", "PRICE"):
                digits = 2 if field == "QTY" else 3
                same = to_number(av, digits) == to_number(ev, digits)
            else:
                same = to_text(ev) == to_text(av)
            if not same:
                diffs.append((ord_no, field, ev, av))
    return common, diffs


def group_diffs(diffs):
    """
    依欄位分組差異，並為每組統計型態。

    診斷重點：多筆出現同一種型態（例如全部把 0 認成 O）通常代表單一
    系統性錯誤，而非隨機失真。回傳 {欄位: {"型態": [(ORD_NO, 預期, 實際), ...]}}。
    """
    grouped = {}
    for ord_no, field, ev, av in diffs:
        bucket = grouped.setdefault(field, {})
        bucket.setdefault(_diff_shape(ev, av), []).append((ord_no, ev, av))
    return grouped


def _diff_shape(ev, av):
    """把一組預期/實際值轉為可比較的型態字串，用於統計重複型態。"""
    if ev is None or av is None or ev == "" or av == "":
        return "一方為空: 預期=%r 實際=%r" % (ev, av)
    ev_s, av_s = to_text(ev), to_text(av)
    if len(ev_s) == len(av_s):
        # 等長時逐字元比對，0 與 O 這類替換會顯示為 ^ 標記
        marks = "".join("^" if a != b else " " for a, b in zip(ev_s, av_s))
        if marks.strip():
            return "等長替換: %r -> %r" % (ev_s, av_s)
    if ev_s.replace(",", "") == av_s.replace(",", ""):
        return "千分位差異: %r -> %r" % (ev_s, av_s)
    if "".join(ch for ch in ev_s if ch.isdigit()) == \
            "".join(ch for ch in av_s if ch.isdigit()):
        return "數字部分相同: %r -> %r" % (ev_s, av_s)
    return "不同長度或內容: %r -> %r" % (ev_s, av_s)


def format_all_diffs(diffs, group=True):
    """輸出完整差異清單，預設依欄位分組並標註重複型態。"""
    lines = []
    if not diffs:
        return ["  （無差異）"]

    if not group:
        for ord_no, field, ev, av in diffs:
            lines.append("  %-10s %-10s 預期=%r" % (ord_no, field, ev))
            lines.append("  %-10s %-10s 實際=%r" % ("", "", av))
        return lines

    grouped = group_diffs(diffs)
    order = [f for f in FIELDS if f in grouped]
    order += [f for f in grouped if f not in order]

    for field in order:
        shapes = grouped[field]
        field_total = sum(len(v) for v in shapes.values())
        lines.append("")
        lines.append("--- %s (%d 處，%d 種型態) ---"
                     % (field, field_total, len(shapes)))
        # 型態多的先排，讓最嚴重的問題浮現
        for shape, items in sorted(shapes.items(), key=lambda kv: -len(kv[1])):
            lines.append("  [%d 筆] %s" % (len(items), shape))
            for ord_no, ev, av in items:
                lines.append("      %-10s 預期=%r  實際=%r" % (ord_no, ev, av))
    return lines


def compare(expected, actual, show=10, full=False, group=True):
    """
    以 ORD_NO 為鍵比對兩份結果，產出逐欄正確率報告。

    show  控制差異明細顯示筆數（None 或負值代表全部）
    full  True 時輸出完整差異清單（依欄位分組），供報告檔留存
    """
    lines = []
    exp_rows = {to_text(r.get("ORD_NO")): r for r in expected.get("data", [])}
    act_rows = {}
    for row in actual.get("data", []):
        act_rows[to_text(row.get("ORD_NO"))] = row

    lines.append("recordCount: 預期 %s / 實際 %s"
                 % (expected.get("recordCount"), actual.get("recordCount")))
    exp_ords, act_ords = set(exp_rows), set(act_rows)
    lines.append("ORD_NO 命中 %d / 預期 %d" % (len(exp_ords & act_ords), len(exp_ords)))
    missing = sorted(exp_ords - act_ords)
    extra = sorted(act_ords - exp_ords)
    lines.append("漏掉的 ORD_NO (%d): %s" % (len(missing), ", ".join(missing) or "無"))
    lines.append("多出的 ORD_NO (%d): %s" % (len(extra), ", ".join(extra) or "無"))

    common, diffs = diff_rows(expected, actual)
    total_cells = len(common) * len(FIELDS)
    field_bad = {f: 0 for f in FIELDS}
    for _ord_no, field, _ev, _av in diffs:
        field_bad[field] += 1

    correct = total_cells - len(diffs)
    pct = (correct / total_cells * 100) if total_cells else 0.0
    lines.append("")
    lines.append("逐欄正確率 (僅計 ORD_NO 命中的 %d 筆 x %d 欄 = %d 格): %.1f%%"
                 % (len(common), len(FIELDS), total_cells, pct))
    lines.append("%-12s %8s %8s" % ("欄位", "錯誤", "正確率"))
    for field in FIELDS:
        bad = field_bad[field]
        rate = 100.0 if not common else (len(common) - bad) / len(common) * 100
        flag = "  <-- 全錯" if common and bad == len(common) else ""
        lines.append("%-12s %8d %7.1f%%%s" % (field, bad, rate, flag))

    if not diffs:
        lines.append("")
        lines.append("差異明細: 無差異")
        return "\n".join(lines)

    lines.append("")
    lines.append("差異總數: %d 處" % len(diffs))

    if full:
        lines.append("")
        lines.append("=== 完整差異明細（依欄位分組，型態由多到少）===")
        lines.extend(format_all_diffs(diffs, group=group))
        return "\n".join(lines)

    limit = len(diffs) if (show is None or show < 0) else show
    if group:
        lines.append("")
        lines.append("差異型態摘要:")
        for field, shapes in sorted(group_diffs(diffs).items(),
                                    key=lambda kv: -sum(len(v) for v in kv[1].values())):
            for shape, items in sorted(shapes.items(), key=lambda kv: -len(kv[1])):
                lines.append("  %-10s [%d 筆] %s" % (field, len(items), shape))

    lines.append("")
    lines.append("差異明細 (前 %d 筆，共 %d 處):"
                 % (min(limit, len(diffs)), len(diffs)))
    for ord_no, field, ev, av in diffs[:limit]:
        lines.append("  %s  %-10s 預期=%r  實際=%r" % (ord_no, field, ev, av))
    if len(diffs) > limit:
        lines.append("  ... 另有 %d 處（報告檔會列出完整內容）" % (len(diffs) - limit))
    return "\n".join(lines)
