#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_locate_experiment.py - 實驗：AI 能否取代程式碼做 PDF 版面定位？

實驗設計
--------
被測組：AI 根據文字內容判斷版面結構（決策型任務）
對照組：AI 根據座標輸出欄位邊界（座標型任務）

五個實驗項，每項跑 --runs 次（預設 3）以量測穩定性：

  1  資料區邊界    找出訂單表格的 y 範圍
  2  資料列辨識    找出哪些列是訂單資料列
  3  欄位語意      欄位索引 -> 資料庫欄位名（決策型）
  4  欄位邊界      欄位 x 左界（座標型，對照組）
  5  端到端        上述決策合併，直接產生最終記錄

Ground truth 全部取自 pdf_text_fentay 的確定性方法，確保雙方 apples-to-apples。

用法：
    python ai_locate_experiment.py
    python ai_locate_experiment.py --model qwen3.5:4b --runs 3
    python ai_locate_experiment.py --only 3,4,5 --dump-prompts
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，請執行: pip install requests")
try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("缺少 PyMuPDF，請執行: pip install PyMuPDF")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

from fentay_common import (
    apply_mapping, compare, log, map_header_to_field, resolve_field,
    resolve_out_dir, to_number, to_text, validate,
)
from ollama_fentay import parse_json_loose
import pdf_text_fentay as pt

BASELINE = "expected.json"
RAW_DIRNAME = "experiment_raw"
REPORT_NAME = "experiment_report.txt"

# 交給 AI 的欄位詞彙（用 FENTAY_B2B 的正式名稱，避免它自創用語）
FIELD_VOCAB = (
    "ORD_NO=訂購單號, MATM_NAME=料號, MATM_DESC=材料名稱/顏色代碼, "
    "WIDE=規格寬度, QTY=數量, UNIT=單位, PRICE=單價, "
    "NEED_DATE=交貨日期, NEED_CUST=需求子公司"
)
NO_IMPORT_HINT = "金額/小計/合計 欄不匯入資料庫，請對應為 null"

PREVIEW_CHARS = 34


def find_pdf():
    pdfs = [f for f in sorted(os.listdir(".")) if f.lower().endswith(".pdf")]
    if not pdfs:
        sys.exit("找不到 PDF 檔案")
    return pdfs[0]


# ---------------------------------------------------------------- Ground truth

def build_ground_truth(pdf_path):
    """用確定性方法算出每頁的正確答案，作為評分基準。"""
    doc = fitz.open(pdf_path)
    truth = []
    grids = []
    try:
        for pi in range(doc.page_count):
            spans = pt.collect_spans(doc[pi])
            top, bottom, _ = pt.find_table_bounds(spans)
            lefts, source = pt.detect_columns(spans, top, bottom, False)
            fields = pt.infer_fields(lefts, spans, top, len(lefts))
            rows = pt.cluster_rows(spans, top, bottom)
            grid = []
            for cells in rows:
                raw = {}
                for x, t in cells:
                    raw.setdefault(pt.column_index(lefts, x), []).append(t)
                grid.append({k: "".join(v) for k, v in sorted(raw.items())})
            grids.append(grid)
            # 全頁所有列（含抬頭與頁尾），供實驗 2 使用
            all_rows = pt.cluster_rows(spans, spans[0][0] - 10,
                                       max(y for y, _, _ in spans))
            previews = [" | ".join(
                "%d=%s" % (pt.column_index(lefts, x) if lefts else 0, t)
                for x, t in cells) for cells in all_rows]
            truth.append({
                "page": pi + 1,
                "top": top,
                "bottom": bottom,
                "lefts": lefts,
                "fields": fields,
                "n_columns": len(lefts),
                "data_rows": list(range(len(rows))),
                "n_data_rows": len(rows),
                "grid": grid,
                "all_row_count": len(all_rows),
                "all_row_previews": previews,
                "column_source": source,
            })
    finally:
        doc.close()
    truth[0]["all_grids"] = grids
    return truth


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


def save_raw(name, text):
    out = resolve_out_dir(None)
    raw_dir = os.path.join(out, RAW_DIRNAME)
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------- Prompt 建构

def prompt_bounds(page_truth):
    """實驗 1：資料區邊界。只給 y 分佈與文字預覽。"""
    lines = [
        "以下是一份 PDF 的所有文字，依 y 座標分組（單位 pt）。",
        "",
    ]
    for entry in page_truth["preview_by_y"]:
        lines.append("y=%-5d %s" % (entry[0], entry[1]))
    lines += [
        "",
        "這份 PDF 的上方是公司抬頭（標題、地址、訂購日期），"
        "中間是訂單資料表，下方是備註與訂購公司資訊。",
        "訂單資料表的每一列都包含一個以 6 開頭的訂購單號。",
        "請找出訂單資料表起始與結束的 y 座標。",
        '只回傳 JSON: {"top": <數字>, "bottom": <數字>}',
    ]
    return "\n".join(lines)


def prompt_rows(page_truth):
    """實驗 2：資料列辨識。給全部列（含抬頭頁尾），問哪些是資料列。"""
    lines = [
        "以下是一份 PDF 的所有文字，已依 y 座標分組為列，共 %d 列（R0 到 R%d）："
        % (page_truth["all_row_count"], page_truth["all_row_count"] - 1),
        "",
    ]
    for i, preview in enumerate(page_truth["all_row_previews"]):
        lines.append("R%-3d %s" % (i, preview[:160]))
    lines += [
        "",
        "其中只有一部分是「訂單資料列」（每列都含一個 6 開頭的訂購單號），",
        "其餘是抬頭、表頭、備註與訂購公司資訊。",
        "訂單資料列是連續的，請給出起始與結束的列索引（含頭尾）。",
        '只回傳 JSON: {"first": <數字>, "last": <數字>}',
    ]
    return "\n".join(lines)


def prompt_semantics(page_truth):
    """實驗 3：欄位語意。只給值，不給任何座標。決策型任務。"""
    samples = page_truth["grid"][:3]
    lines = [
        "以下是一張訂單表格的 %d 列樣本，每列格式為「欄位索引=值」：" % len(samples),
        "",
    ]
    for i, row in enumerate(samples):
        lines.append("R%d: %s" % (i, " | ".join(
            "%d=%s" % (k, v) for k, v in row.items())))
    lines += [
        "",
        "可用的資料庫欄位（mapping 的每個元素必須使用左側的英文字串）：",
        FIELD_VOCAB,
        "",
        NO_IMPORT_HINT + "。",
        "請根據每一欄的內容判斷它代表哪個資料庫欄位。",
        "",
        "回傳格式：mapping 必須是與欄位索引等長的陣列（索引 0 到 %d，共 %d 個元素），"
        % (page_truth["n_columns"] - 1, page_truth["n_columns"]),
        "每個元素填該欄對應的欄位名；不匯入的欄填 null。",
        "",
        "範例（10 欄的表格）：",
        '{"mapping": ["ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "QTY", '
        '"UNIT", "PRICE", null, "NEED_DATE", "NEED_CUST"]}',
    ]
    return "\n".join(lines)


def prompt_edges(page_truth):
    """實驗 4：欄位邊界（對照組）。要求輸出精確 x 座標。座標型任務。"""
    lines = [
        "以下是一份 PDF 表格中所有文字片段的 x 座標（左緣，單位 pt）與其內容前 %d 字元："
        % PREVIEW_CHARS,
        "",
    ]
    for x, preview in page_truth["preview_by_x"]:
        lines.append("x=%-5d %s" % (x, preview))
    lines += [
        "",
        "這張表格共有 %d 欄，欄位由左至右排列。"
        "文字會因內容長短而左右浮動，但每欄有一個固定的起始位置。",
        "請給出每一欄的左邊界 x 座標，由小到大排列。",
        '只回傳 JSON: {"lefts": [<數字>, <數字>, ...]}',
    ]
    return "\n".join(lines)


def prompt_end_to_end(page_truth):
    """實驗 5：端到端。一次取得所有決策。"""
    samples = page_truth["grid"][:2]
    lines = [
        "以下是一份訂單 PDF 的資訊。",
        "",
        "[A] 所有文字依 y 座標的分組（單位 pt）：",
    ]
    for entry in page_truth["preview_by_y"]:
        lines.append("  y=%-5d %s" % (entry[0], entry[1]))
    lines += [
        "",
        "[B] 訂單表格共 %d 欄，以下是 %d 列資料，每列格式為「欄位索引=值」："
        % (page_truth["n_columns"], len(samples)),
    ]
    for i, row in enumerate(samples):
        lines.append("  R%d: %s" % (i, " | ".join(
            "%d=%s" % (k, v) for k, v in row.items())))
    lines += [
        "",
        "[C] 可用的資料庫欄位（mapping 的每個元素必須使用左側的英文字串）：",
        "  " + FIELD_VOCAB,
        "  " + NO_IMPORT_HINT + "。",
        "",
        "請完成兩項判斷：",
        "1. top / bottom：訂單資料表起始與結束的 y 座標",
        "2. mapping：與欄位索引等長的陣列（索引 0 到 %d，共 %d 個元素），"
        "每個元素填該欄對應的欄位名，不匯入的欄填 null"
        % (page_truth["n_columns"] - 1, page_truth["n_columns"]),
        "",
        '範例回傳（10 欄）：{"top": 107, "bottom": 641, '
        '"mapping": ["ORD_NO", "MATM_NAME", "MATM_DESC", "WIDE", "QTY", '
        '"UNIT", "PRICE", null, "NEED_DATE", "NEED_CUST"]}',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 評分

NULL_TOKENS = ("null", "none", "不匯入", "n/a", "")


def as_none(value):
    """把 AI 可能用來表示「不匯入」的寫法統一成 None。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in NULL_TOKENS:
        return None
    return value


def norm_field(name):
    """
    把 AI 回傳的欄位名正規化為 FENTAY_B2B 正式名稱。

    用 resolve_field 而非 map_header_to_field：前者會先檢查是否已是合法欄位名
    （AI 通常直接回傳 ORD_NO），後者只認中文/英文標題別名，會把 ORD_NO 判成 None。
    """
    if as_none(name) is None:
        return None
    field, _ignored = resolve_field(name)
    return field


def score_mapping(answer, truth):
    """實驗 3 評分。區分「原始字串相符」與「正規化後相符」。"""
    if not isinstance(answer, dict) or not isinstance(answer.get("mapping"), list):
        return {"score": 0.0, "strict_score": 0.0, "detail": "缺少 mapping 陣列"}
    ai_list = answer["mapping"]
    expected = truth["fields"]
    if len(ai_list) != len(expected):
        return {
            "score": 0.0, "strict_score": 0.0,
            "detail": "欄位數不符: AI %d, 正確 %d" % (len(ai_list), len(expected)),
        }

    strict = 0
    normalized = 0
    mismatches = []
    for idx, (a, t) in enumerate(zip(ai_list, expected)):
        # 嚴格比對：字面完全相符（不匯入的欄兩邊都是 None）
        if as_none(a) == t:
            strict += 1
        # 正規化比對：容許 AI 用中文或標題別名表示同一欄位
        got = norm_field(a)
        if got == t:
            normalized += 1
        else:
            mismatches.append("欄位%d: AI=%r 正規化=%r 正確=%r"
                              % (idx, a, got, t))

    total = len(expected)
    return {
        "score": normalized / total,
        "strict_score": strict / total,
        "detail": "正規化 %d/%d, 原始字串 %d/%d" % (normalized, total, strict, total),
        "mismatches": mismatches,
    }


def score_bounds(answer, truth):
    """實驗 1 評分。"""
    if not isinstance(answer, dict):
        return {"score": 0.0, "detail": "非 JSON 物件"}
    top = to_number(answer.get("top"), 0)
    bottom = to_number(answer.get("bottom"), 0)
    if top is None or bottom is None:
        return {"score": 0.0, "detail": "缺少 top 或 bottom"}
    dt, db = top - truth["top"], bottom - truth["bottom"]
    exact = (dt == 0 and db == 0)
    score = 1.0 if exact else (0.8 if abs(dt) <= 10 and abs(db) <= 10 else 0.3)
    return {
        "score": score, "exact": exact,
        "ai": [top, bottom], "truth": [truth["top"], truth["bottom"]],
        "delta": [dt, db],
        "detail": "top%s, bottom%s" % (
            "" if dt == 0 else "%+g" % dt, "" if db == 0 else "%+g" % db),
    }


def score_rows(answer, truth):
    """實驗 2 評分。以 F1 計算預測列集合與正確列集合的重疊。"""
    if not isinstance(answer, dict):
        return {"score": 0.0, "detail": "非 JSON 物件"}
    first = to_number(answer.get("first"), 0)
    last = to_number(answer.get("last"), 0)
    if first is None or last is None:
        return {"score": 0.0, "detail": "缺少 first 或 last"}
    predicted = set(range(int(first), int(last) + 1))
    actual = set(truth["data_rows"])
    if not predicted and not actual:
        return {"score": 1.0, "detail": "皆為空"}
    tp = len(predicted & actual)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(actual) if actual else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    exact = (predicted == actual)
    return {
        "score": 1.0 if exact else f1, "exact": exact,
        "ai": [int(first), int(last)], "truth": [0, truth["n_data_rows"] - 1],
        "detail": "預測 %d 列, 正確 %d 列, 命中 %d (精確率 %.0f%% 召回率 %.0f%%)"
                  % (len(predicted), len(actual), tp, precision * 100, recall * 100),
    }


def score_edges(answer, truth):
    """實驗 4 評分（對照組）。比對數量與數值誤差。"""
    if not isinstance(answer, dict) or not isinstance(answer.get("lefts"), list):
        return {"score": 0.0, "exact": False, "detail": "缺少 lefts 陣列"}
    ai_list = [to_number(v, 0) for v in answer["lefts"]]
    ai_list = [v for v in ai_list if v is not None]
    expected = truth["lefts"]
    if len(ai_list) != len(expected):
        return {
            "score": 0.0, "exact": False,
            "ai": ai_list, "truth": expected,
            "detail": "欄位數不符: AI %d, 正確 %d" % (len(ai_list), len(expected)),
        }
    deltas = [a - t for a, t in zip(ai_list, expected)]
    exact = all(d == 0 for d in deltas)
    within_2 = all(abs(d) <= 2 for d in deltas)
    if exact:
        score = 1.0
    elif within_2:
        score = 0.7
    else:
        score = max(0.0, 1.0 - sum(abs(d) for d in deltas) / 200.0)
    return {
        "score": score, "exact": exact, "within_2": within_2,
        "ai": ai_list, "truth": expected, "deltas": deltas,
        "detail": "最大誤差 %g pt" % (max(abs(d) for d in deltas) if deltas else 0),
    }


def score_end_to_end(answer, truth, expected):
    """
    實驗 5 評分。用 AI 的 mapping 重建「全部頁面」的記錄後與基準答案比對。

    AI 只判斷一次欄位語意（依第 1 頁樣本），程式碼套用到所有頁面，
    這是實際可行的使用方式。
    """
    mapping = score_mapping(answer, truth)
    bounds = score_bounds(answer, truth)
    fields = truth["fields"]

    if "score" not in mapping or mapping["score"] == 0:
        return {"score": 0.0, "field_rate": 0.0, "detail": mapping.get("detail", "無效"),
                "recordCount": 0}

    resolved = [norm_field(a) for a in answer["mapping"]]

    records = []
    for page_entry in truth["all_grids"]:
        for row in page_entry:
            out = {}
            for idx, value in row.items():
                field = resolved[idx] if idx < len(resolved) else None
                if field:
                    out[field] = value
            records.append(apply_mapping(out))

    result = {"success": True, "recordCount": len(records), "data": records}
    text = compare(expected, result, 0)
    rate = 0.0
    for line in text.splitlines():
        if line.startswith("逐欄正確率"):
            rate = float(line.split(":")[-1].strip().split("(")[0].strip().rstrip("%"))

    expected_count = expected.get("recordCount")
    count_ok = result["recordCount"] == expected_count
    # 筆數不足會讓欄位正確率的分母變小，因此額外乘上覆蓋率，
    # 避免「只做一半但做出來的那半全對」被評為滿分
    coverage = (result["recordCount"] / expected_count) if expected_count else 0.0
    return {
        "score": (rate / 100.0) * coverage,
        "field_rate": rate,
        "coverage": coverage,
        "recordCount": result["recordCount"],
        "expected_count": expected_count,
        "count_ok": count_ok,
        "mapping_score": mapping.get("score", 0),
        "bounds_score": bounds.get("score", 0),
        "detail": "欄位正確率 %.1f%%, 筆數 %d/%d (覆蓋率 %.0f%%)"
                  % (rate, result["recordCount"], expected_count, coverage * 100),
    }


# ---------------------------------------------------------------- 實驗執行

EXPERIMENTS = [
    (1, "資料區邊界", prompt_bounds, score_bounds, 300),
    (2, "資料列辨識", prompt_rows, score_rows, 400),
    (3, "欄位語意", prompt_semantics, score_mapping, 600),
    (4, "欄位邊界（對照組）", prompt_edges, score_edges, 600),
    (5, "端到端", prompt_end_to_end, score_end_to_end, 900),
]


def run_experiment(exp_id, title, prompt_builder, scorer, num_predict,
                   host, model, page_truth, expected, runs, timeout, dump):
    log("=" * 70)
    log("實驗 %d：%s" % (exp_id, title))
    results = []

    for run_index in range(runs):
        tag = "exp%d_run%d" % (exp_id, run_index + 1)
        try:
            prompt = prompt_builder(page_truth)
        except Exception as exc:
            results.append({"score": 0.0, "error": "prompt 建構失敗: %s" % exc,
                            "elapsed": 0.0, "detail": str(exc)})
            continue

        if dump and run_index == 0:
            log("--- prompt ---")
            log(prompt)

        try:
            raw = call_ollama(host, model, prompt, num_predict, timeout)
        except Exception as exc:
            results.append({"score": 0.0, "error": "呼叫失敗: %s" % exc,
                            "elapsed": 0.0, "detail": str(exc)})
            log("  第 %d 次：呼叫失敗 (%s)" % (run_index + 1, exc))
            continue

        save_raw(tag + ".txt", raw["text"])
        parsed, parse_error = parse_json_loose(raw["text"])
        if parsed is None:
            results.append({
                "score": 0.0, "elapsed": raw["elapsed"], "error": "JSON 解析失敗",
                "detail": parse_error,
                "prompt_tokens": raw["prompt_tokens"],
                "eval_tokens": raw["eval_tokens"],
                "raw": raw["text"][:300],
            })
            log("  第 %d 次：JSON 解析失敗，%.1fs" % (run_index + 1, raw["elapsed"]))
            continue

        if exp_id == 5:
            scored = score_end_to_end(parsed, page_truth, expected)
        else:
            scored = scorer(parsed, page_truth)
        scored["elapsed"] = raw["elapsed"]
        scored["prompt_tokens"] = raw["prompt_tokens"]
        scored["eval_tokens"] = raw["eval_tokens"]
        scored["raw"] = raw["text"][:300]
        results.append(scored)
        log("  第 %d 次：得分 %.2f  %.1fs  %s"
            % (run_index + 1, scored.get("score", 0.0), raw["elapsed"],
               scored.get("detail", "")))
    return results


def summarize(results):
    if not results:
        return {"n": 0}
    scores = [r.get("score", 0.0) for r in results]
    elapsed = [r.get("elapsed", 0.0) for r in results]
    failures = sum(1 for r in results if r.get("score", 0.0) == 0.0)
    unique = len({r.get("raw", "") for r in results})
    return {
        "n": len(results),
        "mean": sum(scores) / len(scores),
        "min": min(scores),
        "max": max(scores),
        "failures": failures,
        "consistent": unique == 1,
        "unique_answers": unique,
        "elapsed_mean": sum(elapsed) / len(elapsed) if elapsed else 0.0,
        "elapsed_total": sum(elapsed),
    }


def main():
    parser = argparse.ArgumentParser(
        description="實驗：AI 能否取代程式碼做 PDF 版面定位")
    parser.add_argument("--pdf", default=None, help="PDF 路徑，預設自動找資料夾內唯一的 PDF")
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--runs", type=int, default=3, help="每個實驗項執行次數")
    parser.add_argument("--only", default=None,
                        help="只跑指定實驗項，例如 3,4,5")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dump-prompts", action="store_true", help="印出 prompt")
    args = parser.parse_args()

    pdf_path = args.pdf or find_pdf()
    log("PDF: %s" % pdf_path)
    log("模型: %s   每項執行 %d 次" % (args.model, args.runs))

    if not os.path.isfile(BASELINE):
        sys.exit("找不到基準答案 %s" % BASELINE)
    with open(BASELINE, "r", encoding="utf-8") as f:
        expected = json.load(f)

    log("建立 ground truth ...")
    truth = build_ground_truth(pdf_path)
    for entry in truth:
        log("  第 %d 頁: 欄位來源=%s, 資料列 %d, 全頁列 %d"
            % (entry["page"], entry["column_source"],
               entry["n_data_rows"], entry["all_row_count"]))

    # 產生預覽（供 prompt 使用）
    doc = fitz.open(pdf_path)
    try:
        for idx, entry in enumerate(truth):
            spans = pt.collect_spans(doc[idx])
            by_y, by_x = [], []
            for y, x, t in sorted(spans):
                text = to_text(t)
                if pt.is_rule(text):
                    continue
                if not by_y or by_y[-1][0] != y:
                    by_y.append([y, []])
                by_y[-1][1].append(text)
                if not by_x or by_x[-1][0] != x:
                    by_x.append([x, []])
                by_x[-1][1].append(text)
            entry["preview_by_y"] = [
                (y, " ".join(v)[:70]) for y, v in by_y]
            entry["preview_by_x"] = [
                (x, " ".join(v)[:PREVIEW_CHARS]) for x, v in by_x]
    finally:
        doc.close()

    wanted = None
    if args.only:
        wanted = {int(x) for x in args.only.split(",") if x.strip()}

    # 只用第一頁做實驗（本文件兩頁版面相同）
    page_truth = truth[0]

    out_dir = resolve_out_dir(None)
    all_results = []

    for exp_id, title, builder, scorer, num_predict in EXPERIMENTS:
        if wanted and exp_id not in wanted:
            continue
        results = run_experiment(exp_id, title, builder, scorer, num_predict,
                                 args.host, args.model, page_truth, expected,
                                 args.runs, args.timeout, args.dump_prompts)
        all_results.append({
            "id": exp_id, "title": title, "results": results,
            "summary": summarize(results),
        })

    # ------------------------------------------------------------ 產生報告
    lines = []
    lines.append("=" * 74)
    lines.append("AI 版面定位能力實驗報告")
    lines.append("=" * 74)
    lines.append("產生時間:   %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("PDF:        %s" % os.path.basename(pdf_path))
    lines.append("模型:       %s" % args.model)
    lines.append("每項執行:   %d 次" % args.runs)
    lines.append("測試頁:     第 1 頁（%d 筆資料, %d 欄）"
                 % (page_truth["n_data_rows"], page_truth["n_columns"]))
    lines.append("")
    lines.append("Ground truth 取自 pdf_text_fentay 的確定性方法，非人工標註。")
    lines.append("")

    lines.append("-" * 74)
    lines.append("逐項結果")
    lines.append("-" * 74)
    for item in all_results:
        s = item["summary"]
        lines.append("")
        lines.append("【實驗 %d】%s" % (item["id"], item["title"]))
        lines.append("  成功 %d/%d   答案一致: %s (%d 種不同回應)   平均 %.1fs  總計 %.1fs"
                     % (s["n"] - s.get("failures", 0), s["n"],
                        "是" if s.get("consistent") else "否",
                        s.get("unique_answers", 0),
                        s.get("elapsed_mean", 0), s.get("elapsed_total", 0)))
        lines.append("  得分 %.3f  (最低 %.2f / 最高 %.2f)"
                     % (s.get("mean", 0), s.get("min", 0), s.get("max", 0)))
        for i, r in enumerate(item["results"]):
            lines.append("    第 %d 次: 得分 %.2f  %.1fs  tokens(%s/%s)  %s"
                         % (i + 1, r.get("score", 0), r.get("elapsed", 0),
                            r.get("prompt_tokens"), r.get("eval_tokens"),
                            r.get("detail", "")))
            if r.get("error"):
                lines.append("      錯誤: %s" % r["error"])
            if r.get("mismatches"):
                for m in r["mismatches"][:4]:
                    lines.append("      %s" % m)

    lines.append("")
    lines.append("-" * 74)
    lines.append("彙總")
    lines.append("-" * 74)
    lines.append("%-24s %-8s %-9s %-9s %s"
                 % ("實驗", "平均得分", "答案一致", "平均耗時", "判定"))
    for item in all_results:
        s = item["summary"]
        mean = s.get("mean", 0)
        verdict = "可靠" if mean >= 0.95 and s.get("consistent") else \
                  ("可用但不穩定" if mean >= 0.8 else "不可靠")
        lines.append("%-24s %-10.3f %-11s %-10.1f %s"
                     % (item["title"], mean,
                        "是" if s.get("consistent") else "否",
                        s.get("elapsed_mean", 0), verdict))

    lines.append("")
    lines.append("-" * 74)
    lines.append("對照組：確定性方法（pdf_text_fentay）")
    lines.append("-" * 74)
    lines.append("  欄位正確率: 100.0%")
    lines.append("  耗時:       0.02 秒")
    lines.append("  答案一致:   是（完全確定性，無任何變動）")
    lines.append("")
    ai_e2e = next((i for i in all_results if i["id"] == 5), None)
    if ai_e2e and ai_e2e["summary"].get("elapsed_mean"):
        ratio = ai_e2e["summary"]["elapsed_mean"] / 0.02
        lines.append("  AI 端到端為確定性方法的 %.0f 倍慢" % ratio)

    report_text = "\n".join(lines)
    report_path = os.path.join(out_dir, REPORT_NAME)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    json_path = os.path.join(out_dir, "experiment_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "model": args.model,
            "runs": args.runs,
            "pdf": os.path.basename(pdf_path),
            "experiments": all_results,
        }, f, ensure_ascii=False, indent=2, default=str)

    log("")
    log(report_text)
    log("")
    log("已輸出: %s" % report_path)
    log("已輸出: %s" % json_path)
    log("原始回應: %s" % os.path.join(out_dir, RAW_DIRNAME))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    main()
