#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ollama_fentay.py - 用 Ollama 解析豐泰(FENTAY) 訂購單 PDF，輸出 FENTAY_B2B 資料表格式 JSON。

用法範例：
    python ollama_fentay.py --pdf ".\\豐泰.pdf" --skill-dir ".\\FENTAY_B2B" \
        --model qwen3.5:4b --model gemma4:12b \
        --mode pure --mode hybrid --compare ".\\expected.json"

設計要點：
  - PDF 一律渲染為 PNG 餵給 vision 模型，不使用 PDF 文字層做資料擷取。
  - SKILL.md / MAPPING.md / EXAMPLES.md 原文注入 prompt，不在程式內重寫規則，
    如此測到的才是 Skill 本身的正確性。
  - mode=pure   : LLM 套用 MAPPING.md 全部規則並輸出最終 JSON（測 Skill 指令是否有效）
  - mode=hybrid : LLM 只輸出原始欄位名與原始值，Python 實作 MAPPING.md 的轉換規則
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import date

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，請執行: pip install requests")
try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("缺少 PyMuPDF，請執行: pip install PyMuPDF")

DEFAULT_HOST = "http://localhost:11434"
SKILL_FILES = ("SKILL.md", "MAPPING.md", "EXAMPLES.md")

FIELDS = ["ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "QTY", "UNIT", "PRICE",
          "NEED_DATE", "NEED_CUST", "BRAND_NO", "CUST_NO", "CONTACT_NO"]

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

ROW_HINT = ("訂購單號 | 料號 | 材料名稱/顏色代碼 | 規格 | 數量 | 單位 | 單價 | "
            "金額 | 交貨日期 | 需求子公司")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


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


# ---------------------------------------------------------------- 日期 / 數值

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
    if raw is None:
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    return str(raw).strip()


def to_wide(raw):
    """WIDE 提取規則：去除雙引號等符號僅保留數字。"""
    digits = re.sub(r"\D", "", to_text(raw))
    return digits


def apply_mapping(row, field_map, dropped):
    """hybrid 模式：套用 MAPPING.md 的轉換規則，產生一筆 FENTAY_B2B 記錄。"""
    record = {f: "" for f in FIELDS}
    record["QTY"] = None
    record["PRICE"] = None

    for header, raw in row.items():
        field, ignored = map_header_to_field(header)
        if ignored:
            dropped.add(to_text(header))
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
            key = to_text(value).replace(" ", "").upper()
            record["UNIT"] = UNIT_MAP.get(key, to_text(value))
        elif field == "NEED_DATE":
            record["NEED_DATE"] = to_date_yyyymmdd(value)
        else:
            record[field] = to_text(value)

    # 欄位預設值規則：固定空字串
    for field in ("BRAND_NO", "CUST_NO", "CONTACT_NO"):
        record[field] = ""
    return record


# ---------------------------------------------------------------- JSON 解析

def strip_fences(text):
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n?```$", text, re.S)
    if fence:
        return fence.group(1).strip()
    return text


def find_balanced(text):
    """取出第一個完整的 { ... } 或 [ ... ] 區段（以引號與跳脫為準）。"""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return None
    start = min(starts)
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:] if depth > 0 else None


def repair_json(text):
    """移除物件/陣列尾逗號，容忍模型常見的小語法錯誤。"""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def parse_json_loose(raw):
    """回傳 (解析結果 或 None, 錯誤訊息)。"""
    text = strip_fences(raw)
    segment = find_balanced(text)
    candidates = [c for c in (segment, text) if c]
    errors = []
    for candidate in candidates:
        for attempt in (candidate, repair_json(candidate)):
            try:
                return json.loads(attempt), None
            except json.JSONDecodeError as exc:
                errors.append("%s (line %d col %d)" % (exc.msg, exc.lineno, exc.colno))
    return None, "; ".join(errors[:3]) or "找不到 JSON 區段"


def coerce_to_result(parsed):
    """把模型各種輸出形狀統一成 {success, recordCount, data[]}。"""
    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict):
        for key in ("data", "rows", "records", "result", "訂單資料", "資料"):
            if isinstance(parsed.get(key), list):
                rows = parsed[key]
                break
        else:
            rows = [parsed]
    else:
        return None, "無法辨識的輸出型別: %s" % type(parsed).__name__

    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            normalized.append({"ORD_NO": to_text(row)})
        else:
            normalized.append(row)
    return {"success": True, "recordCount": len(normalized), "data": normalized}, None


# ---------------------------------------------------------------- PDF 渲染

def render_bands(pdf_path, dpi, crop_top, crop_bottom, bands):
    """把每頁的表格區域渲染成 PNG；回傳 [(page_no, band_index, base64str)]。"""
    doc = fitz.open(pdf_path)
    images = []
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            height = page.rect.height
            top = page.rect.y0 + height * crop_top
            bottom = page.rect.y0 + height * crop_bottom
            if bands <= 1:
                clips = [fitz.Rect(page.rect.x0, top, page.rect.x1, bottom)]
            else:
                span = (bottom - top) / bands
                clips = [fitz.Rect(page.rect.x0, top + i * span,
                                   page.rect.x1, top + (i + 1) * span)
                         for i in range(bands)]
            for band_index, clip in enumerate(clips):
                pixmap = page.get_pixmap(dpi=dpi, clip=clip)
                images.append((page_index + 1, band_index + 1,
                               base64.b64encode(pixmap.tobytes("png")).decode("ascii")))
    finally:
        doc.close()
    return images


# ---------------------------------------------------------------- Prompt

def load_skill(skill_dir):
    parts = []
    for name in SKILL_FILES:
        path = os.path.join(skill_dir, name)
        if not os.path.isfile(path):
            log("[warn] 找不到 %s，略過" % name)
            continue
        with open(path, "r", encoding="utf-8") as f:
            parts.append("===== %s =====\n%s" % (name, f.read()))
    if not parts:
        log("[warn] %s 下找不到任何 Skill 文件，仍將以內建規則繼續" % skill_dir)
    return "\n\n".join(parts)


def build_prompt(mode, skill_text, page_no, band_index, bands):
    header = (
        "你是資料轉換引擎。下面是本次要遵守的 Skill 文件全文，必須完全依其規則處理。\n\n"
        "%s\n\n"
        "===== 本次任務 =====\n"
        "附件圖片是豐泰(FENTAY)訂購單的第 %d 頁" % (skill_text, page_no)
    )
    if bands > 1:
        header += "，且是此頁的第 %d / %d 段（直向切帶）" % (band_index, bands)
    header += "。\n表格欄位順序為：\n%s\n" % ROW_HINT

    if mode == "pure":
        return header + (
            "\n請讀取圖片中「表格資料列」（從第一筆資料到最後一筆，含所有頁面出現的資料列），"
            "依 Skill 文件的規則轉換，直接輸出最終 JSON，結構必須為：\n"
            '{"success": true, "recordCount": <筆數>, "data": [{"ORD_NO":"","MATM_NAME":"",'
            '"MATM_DESC":"","WIDE":"","QTY":0,"UNIT":"","PRICE":0,"NEED_DATE":"",'
            '"NEED_CUST":"","BRAND_NO":"","CUST_NO":"","CONTACT_NO":""}]}\n\n'
            "硬性要求：\n"
            "1. 只輸出 JSON，不要任何說明文字、不要 markdown 程式碼圍籬。\n"
            "2. 圖片中每一筆資料列都必須輸出，不得省略、不得截斷、不得只輸出前幾筆。\n"
            "3. recordCount 必須等於 data 陣列長度。\n"
            "4. 材料名稱含半形雙引號時，必須以反斜線跳脫成 \\\"。\n"
            "5. 備註、地址、訂購公司等表格以外的內容一律忽略。\n"
            "6. 「金額」欄不輸出。\n"
        )

    return header + (
        "\n請只做「讀表」，不要做任何單位、日期、數值轉換。\n"
        "輸出一個 JSON 物件，格式為：\n"
        '{"rows": [{"訂購單號":"","料號":"","材料名稱/顏色代碼":"","規格":"",'
        '"數量":"","單位":"","單價":"","金額":"","交貨日期":"","需求子公司":""}]}\n\n'
        "硬性要求：\n"
        "1. 鍵名必須使用上列的原始中文標題，不要改寫、不要翻譯。\n"
        "2. 值必須是圖片上的原始文字，保留千分位逗號、雙引號與日期斜線，不要轉換格式。\n"
        "3. 圖片中每一筆資料列都必須輸出，不得省略、不得截斷。\n"
        "4. 某欄在該列為空時填空字串。\n"
        "5. 只輸出 JSON，不要說明文字、不要 markdown 圍籬。\n"
    )


# ---------------------------------------------------------------- Ollama 呼叫

def list_models(host):
    response = requests.get("%s/api/tags" % host, timeout=30)
    response.raise_for_status()
    return [m["name"] for m in response.json().get("models", [])]


def call_ollama(host, model, prompt, image_b64, args):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": args.temperature,
            "num_predict": args.num_predict,
            "num_ctx": args.num_ctx,
        },
    }
    if image_b64:
        payload["images"] = [image_b64]
    if args.json_format == "json":
        payload["format"] = "json"

    started = time.time()
    response = requests.post("%s/api/generate" % host, json=payload, timeout=args.timeout)
    response.raise_for_status()
    body = response.json()
    elapsed = time.time() - started
    return body.get("response", ""), {
        "elapsed_sec": round(elapsed, 1),
        "eval_count": body.get("eval_count"),
        "done_reason": body.get("done_reason"),
        "prompt_tokens": body.get("prompt_eval_count"),
    }


# ---------------------------------------------------------------- 驗證 / 比對

def validate(result):
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
                same = to_number(av, 2 if field == "QTY" else 3) == to_number(ev, 2 if field == "QTY" else 3)
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


# ---------------------------------------------------------------- 主流程

def run_one(host, args, model, mode, images, skill_text):
    log("")
    log("=== 模型 %s | 模式 %s ===" % (model, mode))
    records = []
    stats = []
    dropped = set()

    bands = max(1, args.rows_per_call)
    for page_no, band_index, image_b64 in images:
        if args.max_pages and page_no > args.max_pages:
            continue
        prompt = build_prompt(mode, skill_text, page_no, band_index, bands)
        log("  -> 第 %d 頁 第 %d/%d 段，呼叫中..." % (page_no, band_index, bands))
        try:
            raw, stat = call_ollama(host, model, prompt, image_b64, args)
        except requests.RequestException as exc:
            log("  [error] Ollama 呼叫失敗: %s" % exc)
            stats.append({"page": page_no, "band": band_index, "error": str(exc)})
            continue
        stat["page"] = page_no
        stat["band"] = band_index
        stat["raw_chars"] = len(raw)
        stats.append(stat)
        log("     %.1fs, %s tokens, done=%s, 回傳 %d 字元"
            % (stat["elapsed_sec"], stat["eval_count"], stat["done_reason"], len(raw)))

        # 每一頁獨立解析，避免多頁回應串接時只取到第一個 JSON 區段
        parsed, error = parse_json_loose(raw)
        if parsed is None:
            log("  [error] 第 %d 頁 JSON 解析失敗: %s" % (page_no, error))
            raw_path = "%s.%s.%s.p%d.raw.txt" % (
                args.out_base, model.replace(":", "_"), mode, page_no)
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write(raw)
            log("  已輸出原始回應: %s" % raw_path)
            continue

        coerced, error = coerce_to_result(parsed)
        if coerced is None:
            log("  [error] 第 %d 頁 %s" % (page_no, error))
            continue

        if mode == "hybrid":
            for row in coerced["data"]:
                records.append(apply_mapping(row, None, dropped))
        else:
            records.extend(coerced["data"])
        log("     本頁取得 %d 筆" % len(coerced["data"]))

    if dropped:
        log("  已略過不匯入欄位: %s" % ", ".join(sorted(dropped)))
    return {"success": True, "recordCount": len(records), "data": records}, stats, None


def main():
    parser = argparse.ArgumentParser(
        description="用 Ollama 解析豐泰訂購單 PDF 為 FENTAY_B2B JSON")
    parser.add_argument("--pdf", required=True, help="PDF 路徑")
    parser.add_argument("--skill-dir", required=True, help="Skill 目錄（需含 SKILL.md）")
    parser.add_argument("--model", action="append", default=None,
                        help="模型名稱，可重複指定以比較多個模型")
    parser.add_argument("--mode", action="append", default=None,
                        choices=["pure", "hybrid"], help="可重複指定")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--rows-per-call", type=int, default=0,
                        help="每頁切幾段分次呼叫；0 表示整頁一次")
    parser.add_argument("--crop-top", type=float, default=0.06,
                        help="表格區域上界（頁高比例）")
    parser.add_argument("--crop-bottom", type=float, default=0.80,
                        help="表格區域下界（頁高比例）")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--num-predict", type=int, default=16384)
    parser.add_argument("--num-ctx", type=int, default=32768)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--json-format", choices=["json", "none"], default="json")
    parser.add_argument("--out-dir", default=None, help="預設與 PDF 同目錄")
    parser.add_argument("--out-base", default="result", help="輸出檔名前綴")
    parser.add_argument("--compare", default=None, help="基準答案 JSON 路徑")
    parser.add_argument("--show-diffs", type=int, default=10)
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        sys.exit("找不到 PDF: %s" % args.pdf)
    if not os.path.isdir(args.skill_dir):
        sys.exit("找不到 Skill 目錄: %s" % args.skill_dir)

    models = args.model or []
    modes = args.mode or ["pure"]
    if not models:
        try:
            available = list_models(args.host)
        except requests.RequestException as exc:
            sys.exit("無法連線 Ollama (%s): %s" % (args.host, exc))
        sys.exit("請以 --model 指定模型。可用模型：\n  " + "\n  ".join(available))

    expected = None
    if args.compare:
        if not os.path.isfile(args.compare):
            sys.exit("找不到基準答案: %s" % args.compare)
        with open(args.compare, "r", encoding="utf-8") as f:
            expected = json.load(f)

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.pdf))
    if not os.path.isdir(out_dir):
        sys.exit("輸出目錄不存在: %s" % out_dir)
    args.out_base = os.path.join(out_dir, args.out_base)

    skill_text = load_skill(args.skill_dir)
    bands = max(1, args.rows_per_call)
    images = render_bands(args.pdf, args.dpi, args.crop_top, args.crop_bottom, bands)
    log("PDF %s -> %d 張圖 (%d dpi, 每頁 %d 段)"
        % (os.path.basename(args.pdf), len(images), args.dpi, bands))
    log("Skill 載入 %d 個文件" % len(skill_text.split("===== ")) )

    summary = []
    for model in models:
        for mode in modes:
            result, stats, error = run_one(args.host, args, model, mode, images, skill_text)
            problems = validate(result)

            tag = "%s.%s" % (model.replace(":", "_"), mode)
            out_path = "%s.%s.json" % (args.out_base, tag)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            report = ["模型: %s" % model, "模式: %s" % mode,
                      "輸出: %s" % out_path,
                      "recordCount: %s / data 長度: %d"
                      % (result.get("recordCount"), len(result.get("data", []))),
                      "", "驗證問題 (%d):" % len(problems)]
            report.extend("  - " + p for p in problems[:20])
            if expected is not None:
                report.append("")
                report.append("=== 與基準答案比對 ===")
                report.append(compare(expected, result, args.show_diffs))

            report_text = "\n".join(report)
            report_path = "%s.%s.report.txt" % (args.out_base, tag)
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_text)
            log("")
            log(report_text)
            log("")
            log("已輸出: %s / %s" % (out_path, report_path))

            summary.append({
                "model": model, "mode": mode,
                "recordCount": result.get("recordCount"),
                "validation_issues": len(problems),
                "elapsed_sec": round(sum(s.get("elapsed_sec", 0) for s in stats), 1),
                "report": report_path,
            })

    log("")
    log("===== 彙總 =====")
    for item in summary:
        log("  %-14s %-7s recordCount=%-4s 驗證問題=%-3d 耗時=%.1fs"
            % (item["model"], item["mode"], item["recordCount"],
               item["validation_issues"], item["elapsed_sec"]))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
