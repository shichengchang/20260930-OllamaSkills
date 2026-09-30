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


def compare(expected, actual, show=10):
    """以 ORD_NO 為鍵比對兩份結果，產出逐欄正確率報告。"""
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

    common = sorted(exp_ords & act_ords)
    field_bad = {f: 0 for f in FIELDS}
    total_cells = len(common) * len(FIELDS)
    diff_samples = []
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
                field_bad[field] += 1
                diff_samples.append((ord_no, field, ev, av))

    correct = total_cells - sum(field_bad.values())
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

    if diff_samples:
        lines.append("")
        lines.append("差異明細 (前 %d 筆，共 %d 處):" % (show, len(diff_samples)))
        for ord_no, field, ev, av in diff_samples[:show]:
            lines.append("  %s  %-10s 預期=%r  實際=%r" % (ord_no, field, ev, av))
        if len(diff_samples) > show:
            lines.append("  ... 另有 %d 處" % (len(diff_samples) - show))
    return "\n".join(lines)
