#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_text_llm_fentay.py - 路線 D：PDF 文字層（座標分組） + LLM 只判斷欄位語意
                        + Python 套用 MAPPING.md 轉換規則。

三條解析路線的差異：
  - 路線 B（ollama_fentay.py）    ：PDF 渲染為 PNG，LLM 從像素轉錄全部資料值
  - 路線 C（pdf_text_fentay.py）  ：PDF 文字層 + 座標分組，欄位語意由 infer_fields 判定
  - 路線 D（本檔）                ：PDF 文字層 + 座標分組，欄位語意由 LLM 判定

設計要點：
  - 不渲染任何圖片、不呼叫 vision 模型，純文字 prompt。
  - 文字層即原始碼，資料值完全精確，因此 LLM 不負責轉錄。讓模型把已精確的
    字元重打一遍只可能造成失真（例如視覺辨識遺失的空格），不會增加任何資訊。
    LLM 只負責一個決策：每個欄位索引代表哪個資料庫欄位。
  - 擷取層完全重用 pdf_text_fentay 的函式，資料值與路線 C 逐字相同；
    兩條路線唯一的差異就是「欄位語意由誰決定」。
  - MAPPING.md 的轉換規則仍由 fentay_common.apply_mapping 執行，不在 prompt
    中重寫，因此規則維持只有一份實作。

量測上的限制（勿誤讀為獨立證據）：
  路線 D 與 expected.json 的比對，只有 10 個欄位對應具備獨立性；資料值層與
  路線 C 同源。因此本路線的「100%」代表欄位語意判斷正確，不代表資料擷取層
  另有獨立證據。真正的獨立證據是 QTY x PRICE ≈ 金額 的交叉驗證。

用法：
    python pdf_text_llm_fentay.py --pdf ".\\豐泰.pdf" \\
        --model qwen3.5:4b --samples 10 --runs 3 \\
        --compare ".\\expected.json" --dump-columns
"""

import argparse
import collections
import glob
import json
import os
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

import pdf_text_fentay as pt
from fentay_common import (
    FIELDS, FIXED_EMPTY_FIELDS, HEADER_ALIASES, HEADER_IGNORED, apply_mapping,
    apply_ocr_fixes, build_output, compare, log, now_iso, resolve_field,
    resolve_out_dir, timestamp_slug, validate,
)
# 重用路線 B 的回應解析與模型清單工具，避免重寫同一份 JSON 容錯邏輯
from ollama_fentay import list_models, parse_json_loose, strip_fences

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_SAMPLES = 10

# 會匯入資料庫的欄位（依 FIELDS 順序，排除固定空字串欄）
IMPORT_FIELDS = [f for f in FIELDS if f not in FIXED_EMPTY_FIELDS]

# 範例 mapping 直接由 pdf_text_fentay.DOCUMENT_COLUMN_ORDER 推導，不手寫。
# 該順序中的 None 代表「金額」欄，依 MAPPING.md 不匯入資料庫。
EXAMPLE_MAPPING = list(pt.DOCUMENT_COLUMN_ORDER)

# 模型可能用來表示「不匯入」的寫法
NULL_TOKENS = ("null", "none", "nil", "n/a", "不匯入", "-", "")


# ---------------------------------------------------------------- 欄位詞彙

def build_field_vocab():
    """
    由 fentay_common 的 FIELDS 與 HEADER_ALIASES 組出欄位詞彙。

    不手寫常數，避免 prompt 與規格文件脫節；別名全部列出，
    是為了涵蓋不同供應商可能使用的標題用詞。
    """
    parts = []
    for field in IMPORT_FIELDS:
        aliases = HEADER_ALIASES.get(field, [])
        parts.append("%s=%s" % (field, "/".join(aliases) if aliases else field))
    return ", ".join(parts)


def build_example_mapping(n_columns):
    """產生與實際欄數等長的 mapping 範例；欄數不同時以其餘匯入欄位補齊。"""
    example = list(EXAMPLE_MAPPING[:n_columns])
    if len(example) < n_columns:
        used = set(example)
        for field in IMPORT_FIELDS:
            if len(example) >= n_columns:
                break
            if field not in used:
                example.append(field)
                used.add(field)
    return json.dumps({"mapping": example}, ensure_ascii=False)


# ---------------------------------------------------------------- 文字層擷取

def extract_grid(pdf_path, max_pages=0, dump=False):
    """
    以座標分組取出每頁的表格資料，回傳 (pages, grids)。

    刻意不呼叫 pt.infer_fields：欄位語意正是本路線要交給 LLM 決策的部分，
    若在此先用確定性方法判斷，LLM 的貢獻將無從量測。

    pages: [{"page", "lefts", "n_columns", "source", "rows"}]
    grids: [[{欄位索引: 該欄值}, ...], ...]  每頁一個串列清單
    """
    doc = fitz.open(pdf_path)
    pages = []
    grids = []
    try:
        last_page = min(doc.page_count, max_pages) if max_pages else doc.page_count
        for page_index in range(last_page):
            spans = pt.collect_spans(doc[page_index])
            top, bottom, _ = pt.find_table_bounds(spans)
            if top is None:
                log("[error] 第 %d 頁找不到訂單資料區，可能是掃描件（無文字層）"
                    % (page_index + 1))
                pages.append({"page": page_index + 1, "lefts": [], "n_columns": 0,
                              "source": "-", "rows": 0, "note": "找不到訂單資料"})
                grids.append([])
                continue

            lefts, source = pt.detect_columns(spans, top, bottom, dump)
            grid = []
            for cells in pt.cluster_rows(spans, top, bottom):
                raw = {}
                for x, text in cells:
                    raw.setdefault(pt.column_index(lefts, x), []).append(text)
                grid.append({k: "".join(v) for k, v in sorted(raw.items())})

            pages.append({
                "page": page_index + 1,
                "lefts": lefts,
                "n_columns": len(lefts),
                "source": source,
                "rows": len(grid),
            })
            grids.append(grid)
            if dump:
                log("  第 %d 頁: %d 列, %d 欄 (%s)"
                    % (page_index + 1, len(grid), len(lefts), source))
    finally:
        doc.close()
    return pages, grids


def choose_column_count(pages):
    """
    決定本路線要套用的欄數：取出現次數最多的欄數。

    同一份 PDF 若各頁欄數不一致，代表版面不統一。此時只處理多數頁，
    其餘頁在套用 mapping 時會被跳過並記錄，避免用錯欄寬的 mapping
    產生靜默錯位的資料。
    """
    counts = [p["n_columns"] for p in pages if p["n_columns"]]
    if not counts:
        return None
    ranked = collections.Counter(counts)
    return ranked.most_common(1)[0][0]


def pick_samples(grids, allowed_pages, limit):
    """
    跨頁輪流取樣至多 limit 列。

    依實測（qwen3.5:4b、10 份真實訂單）：單一 PDF 的欄位內容自我說明時
    1 列就夠，但資料多樣（含千分位、數量級差大）後 10 列才正確。
    因此預設給 10 列，並以輪流方式避免樣本全落在同一頁。
    """
    samples = []
    depth = max((len(grids[i]) for i in allowed_pages), default=0)
    for i in range(depth):
        for page_no in allowed_pages:
            grid = grids[page_no]
            if i < len(grid):
                samples.append(grid[i])
                if len(samples) >= limit:
                    return samples
    return samples


# ---------------------------------------------------------------- Prompt

def build_prompt(samples, n_columns):
    """
    四段式 prompt（缺一不可，實測少了第 ④ 段會誤解 mapping 的語意）：

      ① 樣本列：只給值，不給任何座標
      ② 可用欄位詞彙：明確指定用左側英文字串
      ③ 格式規格：mapping 必須與欄位索引等長，不匯入的欄填 null
      ④ 具體範例：完整輸出，避免模型把陣列元素誤解為「要匯入的欄位索引」
    """
    lines = [
        "以下是一張訂單表格的 %d 列樣本，每列格式為「欄位索引=值」。" % len(samples),
        "欄位索引由左至右，範圍 0 到 %d，共 %d 欄。" % (n_columns - 1, n_columns),
        "",
    ]
    for i, row in enumerate(samples):
        cells = " | ".join("%d=%s" % (k, v) for k, v in sorted(row.items()))
        lines.append("R%d: %s" % (i, cells))
    lines += [
        "",
        "可用的資料庫欄位（mapping 的每個元素必須使用左側的英文字串）：",
        "  " + build_field_vocab(),
        "",
        "%s 等欄位不匯入資料庫，請對應為 null。" % "/".join(HEADER_IGNORED[:3]),
        "請根據每一欄的內容判斷它代表哪個資料庫欄位。",
        "",
        "回傳格式：mapping 必須是與欄位索引等長的陣列"
        "（索引 0 到 %d，共 %d 個元素），每個元素填該欄對應的欄位名；"
        "不匯入的欄填 null。" % (n_columns - 1, n_columns),
        "",
        "範例（%d 欄的表格）：" % n_columns,
        build_example_mapping(n_columns),
        "",
        "只輸出 JSON，不要說明文字、不要 markdown 圍籬。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- mapping 驗證

def parse_mapping(raw, n_columns):
    """
    解析並嚴格驗證模型回傳的 mapping。

    回傳 (mapping 或 None, 錯誤訊息)。驗證刻意嚴格且不提供任何猜測：
    長度不符、欄位名無法解析、或同一欄位名重複出現時一律中止，
    因為這些情況若硬套規則只會產生靜默欄位錯位的資料。
    """
    parsed, error = parse_json_loose(strip_fences(raw))
    if parsed is None:
        return None, "JSON 解析失敗: %s" % error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("mapping"), list):
        return None, "回應缺少 mapping 陣列"

    raw_list = parsed["mapping"]
    if len(raw_list) != n_columns:
        return None, "mapping 長度 %d 與欄數 %d 不符" % (len(raw_list), n_columns)

    mapping = []
    unknown = []
    for item in raw_list:
        text_value = "" if item is None else str(item).strip()
        if text_value.lower() in NULL_TOKENS:
            mapping.append(None)
            continue
        field, _ignored = resolve_field(text_value)
        if field is None or field not in IMPORT_FIELDS:
            unknown.append(text_value)
            continue
        mapping.append(field)

    if unknown:
        return None, "mapping 含無法解析的欄位名: %s" % ", ".join(unknown)

    duplicated = sorted({f for f in mapping if f and mapping.count(f) > 1})
    if duplicated:
        return None, "mapping 有重複欄位名: %s" % ", ".join(duplicated)

    return mapping, None


def vote_mapping(mappings):
    """
    多次執行取多數結果。

    小模型即使 temperature=0 也非確定性，mapping 可能每次不同。
    沒有嚴格多數時回傳錯誤而非任選其一，因為錯的 mapping 會讓整份輸出
    欄位錯位且不會引起任何驗證問題。
    """
    counter = collections.Counter(tuple(m) for m in mappings)
    best, count = counter.most_common(1)[0]
    if count * 2 <= len(mappings):
        detail = "; ".join("%dx %s" % (c, list(k)) for k, c in counter.most_common())
        return None, "多次執行的 mapping 不一致且無多數: %s" % detail
    return list(best), None


def call_ollama(host, model, prompt, args):
    """純文字呼叫，不帶 images 欄位。"""
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
    if args.json_format == "json":
        payload["format"] = "json"

    started = time.time()
    response = requests.post("%s/api/generate" % host, json=payload, timeout=args.timeout)
    response.raise_for_status()
    body = response.json()
    return body.get("response", ""), {
        "elapsed_sec": round(time.time() - started, 1),
        "eval_count": body.get("eval_count"),
        "done_reason": body.get("done_reason"),
        "prompt_tokens": body.get("prompt_eval_count"),
    }


# ---------------------------------------------------------------- 套用規則

def apply_mapping_to_grid(grid, mapping):
    """把以欄位索引為鍵的列轉成以欄位名為鍵，再交給 fentay_common 套規則。"""
    rows = []
    for cells in grid:
        row = {}
        for idx, value in cells.items():
            if idx >= len(mapping):
                continue
            field = mapping[idx]
            if field:
                row[field] = value
        # 以實際欄位名判定資料列，不假設 ORD_NO 一定在欄位 0
        if "ORD_NO" in row:
            rows.append(row)
    return rows


# ---------------------------------------------------------------- 主流程

def run_route_d(host, args, model, pages, grids, n_columns, prompt):
    """
    呼叫 LLM 取得欄位對應並組出結果。

    回傳 (result, diagnostics, error)。error 非 None 時 result 為 None。
    """
    diagnostics = {"runs": [], "mapping": None, "agreed": None, "skipped": []}

    candidates = []
    for run_index in range(args.runs):
        log("  -> 第 %d/%d 次呼叫，判定欄位語意..." % (run_index + 1, args.runs))
        try:
            raw, stat = call_ollama(host, model, prompt, args)
        except requests.RequestException as exc:
            log("  [error] Ollama 呼叫失敗: %s" % exc)
            diagnostics["runs"].append({"run": run_index + 1, "error": str(exc)})
            return None, diagnostics, "Ollama 呼叫失敗: %s" % exc

        mapping, error = parse_mapping(raw, n_columns)
        stat["run"] = run_index + 1
        stat["raw_chars"] = len(raw)
        stat["mapping"] = mapping
        diagnostics["runs"].append(stat)
        if error:
            log("  [error] mapping 無效: %s" % error)
            log("     原始回應: %s" % raw.strip()[:300])
            return None, diagnostics, error

        log("     %.1fs, %s tokens, mapping: %s"
            % (stat["elapsed_sec"], stat["eval_count"], mapping))
        candidates.append(mapping)

    mapping, error = vote_mapping(candidates)
    if mapping is None:
        return None, diagnostics, error
    agreed = len(set(tuple(m) for m in candidates)) == 1
    diagnostics["mapping"] = mapping
    diagnostics["agreed"] = agreed
    if not agreed:
        log("  [warn] 多次執行的 mapping 不完全一致，已取多數結果")

    dropped = set()
    records = []
    for page_no, grid in enumerate(grids):
        if pages[page_no]["n_columns"] != len(mapping):
            log("[warn] 第 %d 頁欄數 %d 與 mapping 的 %d 不符，跳過該頁"
                % (pages[page_no]["page"], pages[page_no]["n_columns"], len(mapping)))
            diagnostics["skipped"].append(pages[page_no]["page"])
            continue
        rows = apply_mapping_to_grid(grid, mapping)
        records.extend(apply_mapping(row, dropped) for row in rows)
        log("  第 %d 頁: %d 筆" % (pages[page_no]["page"], len(rows)))

    if dropped:
        log("  已略過不匯入欄位: %s" % ", ".join(sorted(dropped)))
    diagnostics["dropped"] = sorted(dropped)

    return {"success": True, "recordCount": len(records), "data": records}, diagnostics, None


def mapping_agreement(pdf_path, mapping, max_pages=0):
    """
    比對 LLM 的 mapping 與確定性方法 pt.infer_fields() 的結果。

    這不是「比對基準答案」，而是兩個彼此獨立的欄位判定來源互相比對：
    一個讀表頭文字比對，一個讀欄位內容交給模型判斷。

    因此解讀方向不對稱：
      - 不一致 → 一定是某一方錯了，必須追查
      - 一致   → 只代表兩種做法得到同一答案，**不構成正確性的證據**
    """
    doc = fitz.open(pdf_path)
    try:
        last_page = min(doc.page_count, max_pages) if max_pages else doc.page_count
        for page_index in range(last_page):
            spans = pt.collect_spans(doc[page_index])
            top, bottom, _ = pt.find_table_bounds(spans)
            if top is None:
                continue
            lefts, _source = pt.detect_columns(spans, top, bottom, False)
            if not lefts:
                continue
            return list(pt.infer_fields(lefts, spans, top, len(lefts)))
    finally:
        doc.close()
    return None


def build_report(args, model, result, diagnostics, problems,
                 elapsed, started_at, ocr_fixed, cross_check, agreement,
                 pdf_name, out_path, report_path):
    lines = [
        "產出時間: %s" % result["_meta"]["generatedAt"],
        "開始時間: %s" % started_at,
        "耗時: %.1f 秒" % elapsed,
        "來源: 路線 D (PDF 文字層座標分組 + LLM 判斷欄位語意，規則由 Python 套用)",
        "模型: %s" % model,
        "LLM 職責: 僅判斷欄位語意（不轉錄資料值；文字層已是原始碼）",
        "樣本列數: %d（跨頁輪流取樣）" % args.samples,
        "執行次數: %d，各次 mapping 是否一致: %s"
        % (args.runs, "是" if diagnostics["agreed"] else "否（已取多數）"),
        "輸入 PDF: %s" % pdf_name,
        "欄位對應: %s" % diagnostics["mapping"],
        "OCR 色碼修正: %d 筆（文字層解析，預期為 0）" % ocr_fixed,
        "輸出 JSON: %s" % out_path,
        "  (JSON 為固定檔名，每次執行覆蓋；本報告檔名含時間戳以保留紀錄)",
        "recordCount: %s / data 長度: %d"
        % (result.get("recordCount"), len(result.get("data", []))),
        "",
        "=== 分頁診斷 ===",
    ]
    for page in diagnostics["pages"]:
        if "note" in page:
            lines.append("  第 %d 頁: %s" % (page["page"], page["note"]))
            continue
        lines.append("  第 %d 頁: %d 列, %d 欄 (%s)%s"
                     % (page["page"], page["rows"], page["n_columns"], page["source"],
                        " [已跳過]" if page["page"] in diagnostics["skipped"] else ""))
    if diagnostics["dropped"]:
        lines.append("已略過不匯入欄位: %s" % ", ".join(diagnostics["dropped"]))

    lines += ["", "=== LLM 呼叫明細 ==="]
    for run in diagnostics["runs"]:
        if "error" in run:
            lines.append("  第 %d 次: 失敗 - %s" % (run["run"], run["error"]))
            continue
        lines.append("  第 %d 次: %.1fs, prompt=%s tokens, output=%s tokens, done=%s"
                     % (run["run"], run["elapsed_sec"], run["prompt_tokens"],
                        run["eval_count"], run["done_reason"]))

    lines += ["", "驗證問題 (%d):" % len(problems)]
    lines.extend("  - " + p for p in problems[:20])
    if len(problems) > 20:
        lines.append("  ... 另有 %d 個驗證問題" % (len(problems) - 20))

    lines += [
        "",
        "=== 獨立交叉驗證 (QTY x PRICE vs 金額) ===",
        "此檢查不依賴 expected.json，為資料擷取層的獨立證據。",
        "驗證筆數: %d / %d" % (cross_check["verified"], cross_check["total"]),
        "不一致: %d" % len(cross_check["mismatches"]),
    ]
    for ord_no, calc, amount in cross_check["mismatches"][:10]:
        lines.append("  %s 計算=%s PDF金額=%s" % (ord_no, calc, amount))

    lines += [
        "",
        "=== 與確定性方法 infer_fields() 的交叉比對 ===",
        "注意: 這不是比對基準答案，而是兩個獨立的欄位判定來源互相比對。",
        "      不一致 = 至少一方錯了，必須追查；一致 ≠ 正確，僅代表兩者結論相同。",
    ]
    if agreement is None:
        lines.append("  無法取得 infer_fields() 結果")
    elif agreement["agreed"]:
        lines.append("  一致（%d 欄）" % len(agreement["code_mapping"]))
    else:
        lines.append("  不一致！差異欄位：")
        for item in agreement["diffs"]:
            lines.append("    欄 %d: LLM=%s / 程式=%s" % item)

    lines += ["", "完整報告: %s" % report_path]
    return "\n".join(lines)


def process_pdf(host, args, model, pdf_path, dump):
    """
    處理單一 PDF：擷取文字層 -> LLM 判欄位語意 -> 套規則 -> 交叉驗證。

    回傳 dict；其中 error 非 None 代表未產生結果，其餘欄位僅供彙總使用。
    """
    name = os.path.basename(pdf_path)
    info = {"pdf": name, "model": model, "error": None}

    pages, grids = extract_grid(pdf_path, args.max_pages, dump)
    n_columns = choose_column_count(pages)
    if not n_columns:
        info["error"] = "找不到可用的表格欄位，可能是掃描件（無文字層）"
        return info

    allowed = [i for i, p in enumerate(pages) if p["n_columns"] == n_columns]
    log("表格欄數: %d（符合此欄數的頁: %s）"
        % (n_columns, ", ".join(str(pages[i]["page"]) for i in allowed)))
    for i, page in enumerate(pages):
        if page["n_columns"] and page["n_columns"] != n_columns:
            log("[warn] 第 %d 頁欄數 %d 與多數頁不同，將於套用 mapping 時跳過"
                % (page["page"], page["n_columns"]))

    samples = pick_samples(grids, allowed, max(1, args.samples))
    log("取樣 %d 列給 LLM 判斷欄位語意" % len(samples))
    prompt = build_prompt(samples, n_columns)

    started_at = now_iso()
    started = time.time()
    result, diagnostics, error = run_route_d(
        host, args, model, pages, grids, n_columns, prompt)
    elapsed = time.time() - started

    if error:
        info["error"] = error
        return info

    diagnostics["pages"] = pages
    result["data"], ocr_fixed = apply_ocr_fixes(result.get("data", []))
    if ocr_fixed:
        log("  OCR 失真修正: %d 筆" % ocr_fixed)

    problems = validate(result)

    build_output(
        result,
        source={
            "route": "D",
            "mode": "hybrid",
            "input": "text-layer",
            "llmRole": "column-mapping",
            "model": model,
            "pdf": name,
            "samples": len(samples),
            "mappingRuns": args.runs,
            "mappingAgreed": diagnostics["agreed"],
            "mapping": diagnostics["mapping"],
            "pages": len(allowed),
            "ocrColorCodeFixed": ocr_fixed,
        },
        elapsed_sec=elapsed,
        started_at=started_at,
    )

    # 獨立交叉驗證：重新從 PDF 取「金額」欄，不依賴 expected.json。
    # 註：此處沿用 pdf_text_fentay.collect_amounts，其內部以 infer_fields
    # 定位訂單欄，僅作為金額字典的鍵，不影響本路線受測的欄位 mapping。
    log("  執行獨立交叉驗證 (QTY x PRICE vs 金額)...")
    cross_check = {"verified": 0, "mismatches": [], "total": len(result["data"])}
    try:
        amounts = pt.collect_amounts(pdf_path)
        verified, mismatches = pt.cross_validate(result["data"], amounts)
        cross_check["verified"] = verified
        cross_check["mismatches"] = mismatches
    except Exception as exc:
        log("[warn] 交叉驗證執行失敗: %s" % exc)
        cross_check["error"] = str(exc)

    code_mapping = None
    try:
        code_mapping = mapping_agreement(pdf_path, diagnostics["mapping"],
                                         args.max_pages)
    except Exception as exc:
        log("[warn] infer_fields 交叉比對失敗: %s" % exc)

    agreement = None
    if code_mapping is not None:
        diffs = [(i, diagnostics["mapping"][i], code_mapping[i])
                 for i in range(min(len(code_mapping), len(diagnostics["mapping"])))
                 if code_mapping[i] != diagnostics["mapping"][i]]
        agreement = {
            "agreed": not diffs,
            "code_mapping": code_mapping,
            "diffs": diffs,
        }
        if diffs:
            log("  [warn] 與 infer_fields() 不一致，差異欄位: %s"
                % ", ".join("欄%d(LLM=%s/程式=%s)" % d for d in diffs))
        else:
            log("  與 infer_fields() 一致（%d 欄）" % len(code_mapping))

    info.update({
        "recordCount": result.get("recordCount"),
        "validation_issues": len(problems),
        "cross_verified": cross_check["verified"],
        "cross_mismatches": len(cross_check["mismatches"]),
        "mapping": diagnostics["mapping"],
        "mapping_agreed": None if agreement is None else agreement["agreed"],
        "columns": n_columns,
        "elapsed_sec": round(elapsed, 1),
        # 以下供 main() 在輸出路徑確定後組報告使用
        "result": result,
        "diagnostics": diagnostics,
        "problems": problems,
        "cross_check": cross_check,
        "agreement": agreement,
        "ocr_fixed": ocr_fixed,
        "elapsed": elapsed,
        "started_at": started_at,
    })
    return info


def collect_inputs(args):
    """依 --pdf / --pdf-dir 決定要處理的檔案清單；回傳 (檔案清單, 是否批次)。"""
    if args.pdf:
        return [args.pdf], False
    if args.pdf_dir:
        if not os.path.isdir(args.pdf_dir):
            sys.exit("找不到目錄: %s" % args.pdf_dir)
        paths = sorted(glob.glob(os.path.join(args.pdf_dir, "*.pdf")))
        if not paths:
            sys.exit("目錄內沒有 *.pdf: %s" % args.pdf_dir)
        return paths, True
    sys.exit("請指定 --pdf 或 --pdf-dir")


def write_summary(out_dir, model, infos):
    """
    寫出批次彙總。

    沒有基準答案可比對時，這份檔案就是唯一的整體結果紀錄，
    因此同時輸出文字版（人看）與 JSON 版（程式讀）。
    """
    ok = [i for i in infos if not i["error"]]
    lines = [
        "產出時間: %s" % now_iso(),
        "模型: %s" % model,
        "檔案數: %d（成功 %d，失敗 %d）"
        % (len(infos), len(ok), len(infos) - len(ok)),
        "",
        "%-14s %6s %6s %8s %10s %8s %s"
        % ("PDF", "筆數", "欄數", "交叉驗證", "不一致", "耗時", "與 infer_fields 一致"),
    ]
    for info in infos:
        if info["error"]:
            lines.append("%-14s %s" % (info["pdf"], "失敗: " + info["error"]))
            continue
        verified = "%d/%d" % (info["cross_verified"], info["recordCount"])
        agree = {True: "是", False: "否（需追查）", None: "n/a"}[info["mapping_agreed"]]
        lines.append("%-14s %6d %6d %8s %10d %7.1fs %s"
                     % (os.path.splitext(info["pdf"])[0], info["recordCount"],
                        info["columns"], verified, info["cross_mismatches"],
                        info["elapsed_sec"], agree))

    lines += [
        "",
        "=== 彙總數字 ===",
        "總筆數: %d" % sum(i["recordCount"] for i in ok),
        "交叉驗證通過: %d / %d"
        % (sum(i["cross_verified"] for i in ok),
           sum(i["recordCount"] for i in ok)),
        "交叉驗證不一致: %d" % sum(i["cross_mismatches"] for i in ok),
        "與 infer_fields() 一致的檔案: %d / %d"
        % (sum(1 for i in ok if i["mapping_agreed"] is True), len(ok)),
        "LLM 產生的不同 mapping 種類數: %d"
        % len({tuple(i["mapping"]) for i in ok}),
        "",
        "注意: PDF/ 內沒有基準答案，因此本表沒有『正確率』。",
        "      可用的獨立證據有兩個：",
        "        1) QTY x PRICE ≈ 金額  → 偵測欄位錯位與資料列對錯",
        "        2) 與 infer_fields() 一致 → 兩個獨立判定來源結論相同",
        "      兩者都無法證明欄位語意本身正確，只能證明沒有明顯錯位。",
    ]

    text_path = os.path.join(out_dir, "summary.%s.txt" % model.replace(":", "_"))
    with open(text_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    payload = {
        "model": model,
        "generatedAt": now_iso(),
        "fileCount": len(infos),
        "successCount": len(ok),
        "totalRecords": sum(i["recordCount"] for i in ok),
        "crossVerified": sum(i["cross_verified"] for i in ok),
        "crossMismatches": sum(i["cross_mismatches"] for i in ok),
        "agreedWithInferFields": sum(1 for i in ok if i["mapping_agreed"] is True),
        "distinctMappings": [list(m) for m in {tuple(i["mapping"]) for i in ok}],
        "files": [{k: v for k, v in i.items()
                   if k not in ("result", "diagnostics", "problems",
                                "cross_check", "agreement")} for i in infos],
    }
    json_path = os.path.join(out_dir, "summary.%s.json" % model.replace(":", "_"))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return text_path, json_path


def main():
    parser = argparse.ArgumentParser(
        description="路線 D：PDF 文字層 + LLM 判斷欄位語意，輸出 FENTAY_B2B JSON")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pdf", help="單一 PDF 路徑")
    source.add_argument("--pdf-dir", help="目錄，批次處理其中所有 *.pdf")
    parser.add_argument("--model", action="append", default=None,
                        help="模型名稱，可重複指定以比較多個模型")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help="交給 LLM 判斷欄位語意的樣本列數，預設 %d"
                             % DEFAULT_SAMPLES)
    parser.add_argument("--runs", type=int, default=1,
                        help="LLM 呼叫次數，用於量測非確定性；>1 時取多數結果")
    parser.add_argument("--max-pages", type=int, default=0, help="0 表示全部頁")
    parser.add_argument("--num-predict", type=int, default=512,
                        help="mapping 僅約 61 tokens，預設值遠小於路線 B")
    parser.add_argument("--num-ctx", type=int, default=32768)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--json-format", choices=["json", "none"], default="json")
    parser.add_argument("--out-dir", default=None, help="輸出目錄，預設 output/")
    parser.add_argument("--out-base", default="result",
                        help="輸出檔名前綴（僅單檔模式使用）")
    parser.add_argument("--compare", default=None,
                        help="基準答案 JSON 路徑（僅單檔模式）")
    parser.add_argument("--show-diffs", type=int, default=10,
                        help="console 顯示的差異筆數，-1 代表全部（報告檔一律完整）")
    parser.add_argument("--dump-columns", action="store_true", help="輸出欄位偵測診斷")
    args = parser.parse_args()

    inputs, is_batch = collect_inputs(args)
    if is_batch and args.compare:
        sys.exit("--compare 僅適用於單檔模式；PDF/ 沒有基準答案，"
                 "請改看輸出目錄中的 summary 檔")

    for path in inputs:
        if not os.path.isfile(path):
            sys.exit("找不到 PDF: %s" % path)

    models = args.model or [DEFAULT_MODEL]
    try:
        available = list_models(args.host)
    except requests.RequestException as exc:
        sys.exit("無法連線 Ollama (%s): %s" % (args.host, exc))
    if not set(models) <= set(available):
        sys.exit("指定的模型不在 Ollama 中。可用模型：\n  " + "\n  ".join(available))

    expected = None
    if args.compare:
        if not os.path.isfile(args.compare):
            sys.exit("找不到基準答案: %s" % args.compare)
        with open(args.compare, "r", encoding="utf-8") as f:
            expected = json.load(f)

    out_dir = resolve_out_dir(args.out_dir)
    slug = timestamp_slug()

    if is_batch:
        # 批次結果寫到子目錄，且檔名不以 result. 開頭，
        # 避免被 compare_results.py（glob result.*.json 且假設同一份基準答案）
        # 誤認成 豐泰.pdf 的結果。
        out_dir = os.path.join(out_dir, "pdf_batch")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    out_base = os.path.join(out_dir, args.out_base)

    summary = []
    for model in models:
        log("")
        log("########## 模型 %s | 路線 D（LLM 只判欄位語意） ##########" % model)
        infos = []
        for path in inputs:
            log("")
            log("--- %s ---" % os.path.basename(path))
            info = process_pdf(args.host, args, model, path, args.dump_columns)
            infos.append(info)

            if info["error"]:
                log("  [error] %s" % info["error"])
                continue

            stem = os.path.splitext(os.path.basename(path))[0]
            tag = "%s.textmap" % model.replace(":", "_")
            if is_batch:
                out_path = os.path.join(out_dir, "fentay.%s.%s.json" % (stem, tag))
            else:
                out_path = "%s.%s.json" % (out_base, tag)
            report_path = "%s.%s.report.txt" % (out_path[:-len(".json")], slug)

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(info["result"], f, ensure_ascii=False, indent=2)

            text = build_report(
                args, model, info["result"], info["diagnostics"],
                info["problems"], info["elapsed"], info["started_at"],
                info["ocr_fixed"], info["cross_check"], info["agreement"],
                info["pdf"], out_path, report_path)
            if expected is not None:
                text += ("\n\n=== 與基準答案比對 ===\n"
                         "注意: 本路線的資料值層與路線 C 同源（共用文字層擷取函式），\n"
                         "      因此比對結果只有『欄位語意』具備獨立性，\n"
                         "      資料擷取層的獨立證據請看上面的 QTY x PRICE 交叉驗證。\n\n"
                         + compare(expected, info["result"],
                                   show=args.show_diffs, full=True))
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(text)

            info["out_path"] = out_path
            info["report_path"] = report_path
            log("  已輸出: %s" % out_path)
            log("  完整報告: %s" % report_path)
        summary.extend(infos)

    if is_batch:
        for model in models:
            text_path, json_path = write_summary(out_dir, model, summary)
            log("")
            log("批次彙總: %s" % text_path)
            log("批次彙總(JSON): %s" % json_path)
            log("")
            with open(text_path, "r", encoding="utf-8") as f:
                log(f.read())

    log("===== 彙總 =====")
    log("  產出時間: %s" % now_iso())
    for item in summary:
        if item["error"]:
            log("  %-14s 失敗: %s" % (item["pdf"], item["error"]))
            continue
        log("  %-14s recordCount=%-4s 驗證問題=%-3d 交叉驗證=%s/%s 耗時=%.1fs"
            % (item["pdf"], item["recordCount"], item["validation_issues"],
               item["cross_verified"], item["recordCount"], item["elapsed_sec"]))
    log("  輸出目錄: %s" % out_dir)
    if any(item["error"] for item in summary):
        sys.exit(1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()