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

本檔案只負責「PDF 轉圖 -> 呼叫 Ollama -> 解析回應」；
MAPPING.md 的轉換規則與輸出契約位於 fentay_common.py，與方案 C 共用。
"""

import argparse
import base64
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，請執行: pip install requests")
try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("缺少 PyMuPDF，請執行: pip install PyMuPDF")

from fentay_common import (
    apply_mapping, build_output, compare, log, now_iso, resolve_out_dir,
    timestamp_slug, validate,
)

DEFAULT_HOST = "http://localhost:11434"
SKILL_FILES = ("SKILL.md", "MAPPING.md", "EXAMPLES.md")

ROW_HINT = ("訂購單號 | 料號 | 材料名稱/顏色代碼 | 規格 | 數量 | 單位 | 單價 | "
            "金額 | 交貨日期 | 需求子公司")

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
                normalized.append({"ORD_NO": str(row).strip()})
        else:
            normalized.append(row)
    return {"success": True, "recordCount": len(normalized), "data": normalized}, None


# ---------------------------------------------------------------- PDF 渲染

def render_bands(pdf_path, dpi, crop_top, crop_bottom, bands, max_pages=0):
    """把每頁的表格區域渲染成 PNG；回傳 [(page_no, band_index, base64str)]。"""
    doc = fitz.open(pdf_path)
    images = []
    try:
        last_page = min(doc.page_count, max_pages) if max_pages else doc.page_count
        for page_index in range(last_page):
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
    return "\n\n".join(parts), len(parts)


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
                records.append(apply_mapping(row, dropped))
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
    parser.add_argument("--out-dir", default=None,
                        help="輸出目錄，預設為專案下的 output/")
    parser.add_argument("--out-base", default="result", help="輸出檔名前綴（不含時間戳）")
    parser.add_argument("--compare", default=None, help="基準答案 JSON 路徑")
    parser.add_argument("--show-diffs", type=int, default=10,
                        help="console 顯示的差異筆數，-1 代表全部（報告檔一律完整）")
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

    args.out_base = os.path.join(resolve_out_dir(args.out_dir), args.out_base)

    skill_text, skill_count = load_skill(args.skill_dir)
    bands = max(1, args.rows_per_call)
    images = render_bands(args.pdf, args.dpi, args.crop_top, args.crop_bottom,
                          bands, args.max_pages)
    log("PDF %s -> %d 張圖 (%d dpi, 每頁 %d 段)"
        % (os.path.basename(args.pdf), len(images), args.dpi, bands))
    log("Skill 載入 %d 個文件" % skill_count)

    summary = []
    for model in models:
        for mode in modes:
            started_at = now_iso()
            # 整輪共用同一組 slug，避免同一次執行內各組合時間戳不一致
            slug = timestamp_slug()
            call_started = time.time()
            result, stats, error = run_one(args.host, args, model, mode, images, skill_text)
            elapsed = time.time() - call_started
            problems = validate(result)

            build_output(
                result,
                source={
                    "route": "B",
                    "mode": mode,
                    "model": model,
                    "pdf": os.path.basename(args.pdf),
                    "dpi": args.dpi,
                    "pages": len(images),
                },
                elapsed_sec=elapsed,
                started_at=started_at,
            )

            # JSON 使用固定檔名（每次覆蓋，供下游取用最新結果）
            # 報告檔名帶時間戳，保留每次執行的紀錄
            tag = "%s.%s" % (model.replace(":", "_"), mode)
            out_path = "%s.%s.json" % (args.out_base, tag)
            report_path = "%s.%s.%s.report.txt" % (args.out_base, tag, slug)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            report = [
                "產出時間: %s" % result["_meta"]["generatedAt"],
                "開始時間: %s" % started_at,
                "耗時: %.1f 秒" % elapsed,
                "來源: 路線 B (PDF 轉 PNG 後由 vision 模型解析)",
                "模型: %s" % model,
                "模式: %s" % mode,
                "輸入 PDF: %s (%d dpi, %d 張圖)"
                % (os.path.basename(args.pdf), args.dpi, len(images)),
                "輸出 JSON: %s" % out_path,
                "  (JSON 為固定檔名，每次執行覆蓋；本報告檔名含時間戳以保留紀錄)",
                "recordCount: %s / data 長度: %d"
                % (result.get("recordCount"), len(result.get("data", []))),
                "",
                "驗證問題 (%d):" % len(problems),
            ]
            report.extend("  - " + p for p in problems[:20])
            if len(problems) > 20:
                report.append("  ... 另有 %d 個驗證問題" % (len(problems) - 20))
            if expected is not None:
                report.append("")
                report.append("=== 與基準答案比對 ===")
                # 報告檔寫入完整差異，便於事後診斷每一處錯誤
                report.append(compare(expected, result, show=args.show_diffs,
                                      full=True))

            report_text = "\n".join(report)
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_text)
            # console 顯示精簡版，報告檔才是完整內容
            if expected is not None:
                log("")
                log(compare(expected, result, show=args.show_diffs))
            log("")
            log("已輸出: %s" % out_path)
            log("完整報告: %s" % report_path)

            summary.append({
                "model": model, "mode": mode,
                "recordCount": result.get("recordCount"),
                "validation_issues": len(problems),
                "elapsed_sec": round(elapsed, 1),
                "report": report_path,
            })

    log("")
    log("===== 彙總 =====")
    log("  產出時間: %s" % now_iso())
    for item in summary:
        log("  %-14s %-7s recordCount=%-4s 驗證問題=%-3d 耗時=%.1fs"
            % (item["model"], item["mode"], item["recordCount"],
               item["validation_issues"], item["elapsed_sec"]))
    log("  輸出目錄: %s" % os.path.dirname(args.out_base))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
