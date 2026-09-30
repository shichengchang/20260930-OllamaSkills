# -*- coding: utf-8 -*-
"""ollama_fentay.py 與 pdf_text_fentay.py 的單元測試。"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
FAILED = []


def load(name):
    spec = importlib.util.spec_from_file_location(name, name + ".py")
    mod = importlib.util.module_from_spec(spec)
    # 必須寫入 sys.modules，否則後續 `from fentay_common import ...` 會另建一份
    # 模組物件，導致物件識別（is）比較失敗
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


# 共用層規則測試直接測 fentay_common（規則的唯一實作來源）
m = load("fentay_common")
Q = chr(34)

print("=== 日期格式處理 (MAPPING.md) ===")
ok(m.to_date_yyyymmdd("2026/03/27") == "20260327", "2026/03/27 -> 20260327")
ok(m.to_date_yyyymmdd("2026-03-27") == "20260327", "2026-03-27 -> 20260327")
ok(m.to_date_yyyymmdd("20260327") == "20260327", "20260327 -> 20260327")
ok(m.to_date_yyyymmdd("113/03/27") == "20240327", "民國 113/03/27 -> 20240327")
ok(m.to_date_yyyymmdd("113-03-27") == "20240327", "民國 113-03-27 -> 20240327")
ok(m.to_date_yyyymmdd("") == "", "空字串 -> 空字串")
ok(m.to_date_yyyymmdd("garbage") == "", "無法辨識 -> 空字串")
ok(m.to_date_yyyymmdd(None) == "", "None -> 空字串")

print("=== 數值處理規則 ===")
ok(m.to_number("1,460.0", 2) == 1460.0, "1,460.0 -> 1460.0")
ok(m.to_number("73.00", 3) == 73.0, "73.00 -> 73.0")
ok(m.to_number(" 820.0 ", 2) == 820.0, " 820.0 -> 820.0")
ok(m.to_number("8,432.0", 2) == 8432.0, "8,432.0 -> 8432.0")
ok(m.to_number("2117.0", 2) == 2117.0, "2117.0 -> 2117.0")
ok(m.to_number("", 2) is None, "空字串 -> None")
ok(m.to_number("碼", 2) is None, "非數值 -> None")

print("=== WIDE 提取規則：只保留數字，去除雙引號 ===")
ok(m.to_wide("44" + Q) == "44", '44" -> 44')
ok(m.to_wide("60" + Q) == "60", '60" -> 60')
ok(m.to_wide("40" + Q) == "40", '40" -> 40')
ok(m.to_wide("") == "", "空 -> 空字串")

print("=== 標題比對 ===")
for header, expect in [
    ("訂購單號", "ORD_NO"), ("料號", "MATM_NAME"),
    ("材料名稱/顏色代碼", "MATM_DESC"), ("材料名稱／顏色代碼", "MATM_DESC"),
    ("規格", "WIDE"), ("數量", "QTY"), ("單位", "UNIT"), ("單價", "PRICE"),
    ("交貨日期", "NEED_DATE"), ("需求子公司", "NEED_CUST"),
    ("金額", None), ("Qty", "QTY"), ("PO NO", "ORD_NO"),
    ("需求公司", "NEED_CUST"), ("Unit Price", "PRICE"),
]:
    got, _ = m.map_header_to_field(header)
    ok(got == expect, "%r -> %s (預期 %s)" % (header, got, expect))
for spaced in ["單 價", "規    格", "數     量", "需  求 子公司", "金      額"]:
    got, _ = m.map_header_to_field(spaced)
    ok(got is not None or spaced == "金      額", "含空白標題 %r -> %s" % (spaced, got))

print("=== 模型輸出形狀統一（方案 B 專屬） ===")
b = load("ollama_fentay")
p, e = b.coerce_to_result({"data": [{"ORD_NO": "1"}]})
ok(p["recordCount"] == 1 and e is None, "{data:[...]} 正常")
p, e = b.coerce_to_result([{"ORD_NO": "1"}])
ok(p["recordCount"] == 1, "裸陣列正常")
p, e = b.coerce_to_result({"success": True, "recordCount": 1, "data": []})
ok(p["recordCount"] == 0, "空 data 陣列正常")

print("=== 容錯 JSON 解析（方案 B 專屬） ===")
p, e = b.parse_json_loose('```json\n{"a":[1,2,],}\n```')
ok(p == {"a": [1, 2]} and e is None, "圍籬 + 尾逗號")
p, e = b.parse_json_loose('廢話 {"success":true,"data":[]} 更多廢話')
ok(p == {"success": True, "data": []} and e is None, "前後夾雜說明文字")
p, e = b.parse_json_loose("沒有 json 在這裡")
ok(p is None and e is not None, "無 JSON 時回傳 None 並附錯誤")
p, e = b.parse_json_loose('{"d":"44\\"74F黃"}')
ok(p == {"d": '44"74F黃'}, "含跳脫雙引號的字串")

print("=== apply_mapping: WIDE 去引號 / MATM_DESC 保留引號 ===")
row = {
    "訂購單號": "6AF1101", "料號": "3056G",
    "材料名稱/顏色代碼": "44" + Q + "74F黃LJ-A8-P網布/(74F)",
    "規格": "44" + Q, "數量": " 820.0 ", "單位": "碼", "單價": "73.00",
    "金額": "59,860.00", "交貨日期": "2026/03/27", "需求子公司": "LU1",
}
got = m.apply_mapping(row, set())
expect = {
    "ORD_NO": "6AF1101", "MATM_NAME": "3056G",
    "MATM_DESC": "44" + Q + "74F黃LJ-A8-P網布/(74F)", "WIDE": "44",
    "QTY": 820.0, "UNIT": "YD", "PRICE": 73.0,
    "NEED_DATE": "20260327", "NEED_CUST": "LU1",
    "BRAND_NO": "", "CUST_NO": "", "CONTACT_NO": "",
}
ok(got == expect, "完整列轉換正確")
if got != expect:
    print("    got:", got)
ok(Q not in got["WIDE"], "WIDE 已去除雙引號")
ok(Q in got["MATM_DESC"], "MATM_DESC 保留雙引號")
ok("金額" not in got, "金額欄位未匯入")

print("=== apply_mapping: 缺漏欄位 ===")
got2 = m.apply_mapping({
    "訂購單號": "6AF1200", "材料名稱/顏色代碼": "56" + Q + "P28黑特利",
    "規格": "56" + Q, "數量": "547.0", "單位": "碼", "單價": "7.80",
    "需求子公司": "LU1",
}, set())
ok(got2["MATM_NAME"] == "", "字串欄位缺漏 -> 空字串")
ok(got2["NEED_DATE"] == "", "日期欄位缺漏 -> 空字串")
ok(got2["QTY"] == 547.0, "QTY 仍正確")
ok(all(got2[f] == "" for f in ("BRAND_NO", "CUST_NO", "CONTACT_NO")),
   "預設欄位固定空字串")

print("=== 金額欄略過 ===")
dropped3 = set()
m.apply_mapping({"訂購單號": "X", "金額": "1,000.00", "數量": "1"}, dropped3)
ok("金額" in dropped3, "金額欄位被標記為略過")

ok(len(m.FIELDS) == 12, "FIELDS 共 12 欄")

print("=== OCR 失真修正：色碼 O/0 混淆 ===")
Q3 = chr(34)
fix_cases = [
    # (輸入, 是否應修正, 說明)
    ("OAVLJA4497K9EPM5回網/(0AV)", True, "前綴 OAV 與括號 0AV 不一致 -> 修正為 0AV"),
    ("OBG A2279-2EPM5保利2/(0BG)", True, "前綴 OBG 與括號 0BG 不一致 -> 修正為 0BG"),
    ("0AVLJA4497K9EPM5回網/(0AV)", False, "前綴已正確，不動（路線 C 安全性）"),
    ("06F A2279-2EPM5保利2/(06F)", False, "首字元皆為 0，不一致條件不成立"),
    ("2CQ A2279-2EPM5保利2/(2CQ)", False, "首字元非 0/O，不觸發"),
    ('44"74F黃LJ-A8-P網布/(74F)', False, "非 0/O 開頭，不觸發"),
    ('44" 黑保利2 CDP布/(00A)', False, "無色碼前綴，漏空格不在此規則範圍"),
    ('56"P28黑特利', False, "無括號色碼，不觸發"),
    ("XYZabc/(00A)", False, "字母不同，不觸發"),
    ("", False, "空字串不觸發"),
    (None, False, "None 不觸發"),
]
for value, should_fix, desc in fix_cases:
    got, changed = m.fix_ocr_color_code(value)
    ok(changed == should_fix, desc)

print("=== OCR 修正後的字串正確性 ===")
got, changed = m.fix_ocr_color_code("OAVLJA4497K9EPM5回網/(0AV)")
ok(got == "0AVLJA4497K9EPM5回網/(0AV)", "OAVLJA -> 0AVLJA（只有首字元被改）")
got, changed = m.fix_ocr_color_code("OBG A2279-2EPM5保利2/(0BG)")
ok(got == "0BG A2279-2EPM5保利2/(0BG)", "OBG -> 0BG")
ok(len("OBG A2279-2EPM5保利2/(0BG)") == len("0BG A2279-2EPM5保利2/(0BG)"),
   "修正後字串長度不變")

print("=== apply_ocr_fixes 批次處理 ===")
records = [
    {"ORD_NO": "6AF1105", "MATM_DESC": "OAVLJA4497K9EPM5回網/(0AV)"},
    {"ORD_NO": "6AF1112", "MATM_DESC": "OBG A2279-2EPM5保利2/(0BG)"},
    {"ORD_NO": "6AF1101", "MATM_DESC": '44"74F黃LJ-A8-P網布/(74F)'},
]
records, count = m.apply_ocr_fixes(records)
ok(count == 2, "批次修正 2 筆")
ok(records[0]["MATM_DESC"].startswith("0AV"), "第 1 筆已修正")
ok(records[1]["MATM_DESC"].startswith("0BG"), "第 2 筆已修正")
ok(records[2]["MATM_DESC"] == '44"74F黃LJ-A8-P網布/(74F)', "第 3 筆未變動")

print("=== 對基準答案與路線 C 結果為 no-op ===")
for path in ("expected.json", "output/result.C.json"):
    if not os.path.isfile(path):
        continue
    with open(path, "r", encoding="utf-8") as f:
        before = json.load(f)["data"]
    _, hits = m.apply_ocr_fixes([dict(r) for r in before])
    ok(hits == 0, "%s 不受 OCR 修正影響（%d 筆變動）" % (path, hits))

print("=== 預設 DPI ===")
ok(m.DEFAULT_DPI == 400, "DEFAULT_DPI = 400")

print("=== validate ===")
good = {"success": True, "recordCount": 1, "data": [dict(expect)]}
ok(m.validate(good) == [], "正確資料無驗證問題")
bad = {"success": True, "recordCount": 5, "data": [dict(expect)]}
ok(len(m.validate(bad)) > 0, "recordCount 不一致會被驗證抓出")
bad2 = {"success": True, "recordCount": 1, "data": [{"ORD_NO": "X", "QTY": None}]}
ok(len(m.validate(bad2)) > 0, "QTY 為 null 會被驗證抓出")

print("=== fentay_common: 模組邊界 ===")
ok(hasattr(b, "apply_mapping") and hasattr(b, "compare") and hasattr(b, "validate"),
   "ollama_fentay 由共用層取得規則與輸出契約")
ok(not hasattr(b, "to_wide") and not hasattr(b, "map_header_to_field"),
   "ollama_fentay 已不含規則實作（只 import，不重複定義）")
ok(b.apply_mapping is m.apply_mapping,
   "apply_mapping 為同一個函式物件，非各自複製")

c = load("pdf_text_fentay")
ok(hasattr(c, "apply_mapping") and not hasattr(c, "apply_mapping_by_field"),
   "pdf_text_fentay 使用共用層 apply_mapping")
ok(c.apply_mapping is m.apply_mapping,
   "pdf_text_fentay 與 ollama_fentay 共用同一份規則實作")
ok(not hasattr(b, "cluster_rows") and not hasattr(b, "detect_columns"),
   "PDF 座標解析僅存在於方案 C")
ok(not hasattr(c, "call_ollama") and not hasattr(c, "build_prompt"),
   "Ollama 呼叫與 prompt 組裝僅存在於方案 B")

print("=== 兩條路線對同一列產生相同結果 ===")
Q2 = chr(34)
by_header = b.apply_mapping({
    "訂購單號": "6AF1101", "料號": "3056G",
    "材料名稱/顏色代碼": "44" + Q2 + "74F" + Q2,
    "規格": "44" + Q2, "數量": " 820.0 ", "單位": "碼", "單價": "73.00",
    "金額": "59,860.00", "交貨日期": "2026/03/27", "需求子公司": "LU1",
}, set())
by_field = c.apply_mapping({
    "ORD_NO": "6AF1101", "MATM_NAME": "3056G",
    "MATM_DESC": "44" + Q2 + "74F" + Q2,
    "WIDE": "44" + Q2, "QTY": " 820.0 ", "UNIT": "碼",
    "PRICE": "73.00", "NEED_DATE": "2026/03/27", "NEED_CUST": "LU1",
})
ok(by_header == by_field, "中文標題鍵與欄位名鍵得到完全相同的記錄")

print("=== load_skill 回傳 (文字, 檔案數) ===")
text, count = b.load_skill("FENTAY_B2B")
ok(count == 3, "回傳檔案數為 3 (SKILL/MAPPING/EXAMPLES)")
ok(len(text) > 1000, "Skill 全文長度合理")

print("=== render_bands: max_pages 生效 ===")
imgs_all = b.render_bands("豐泰.pdf", 72, 0.06, 0.80, 1)
imgs_one = b.render_bands("豐泰.pdf", 72, 0.06, 0.80, 1, max_pages=1)
ok(len(imgs_all) == 2, "預設渲染 2 頁")
ok(len(imgs_one) == 1, "max_pages=1 只渲染 1 頁")

print("")
print("=" * 50)
print("共用層與方案 B 測試失敗 %d 項" % len(FAILED))
for f in FAILED:
    print("  - " + f)

print("")
print("=== 方案 C 內建自我檢查 ===")
cfail = []
for cond, msg in c.run_self_tests():
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        cfail.append(msg)
print("")
print("=" * 50)
print("方案 C 測試失敗 %d 項" % len(cfail))
for f in cfail:
    print("  - " + f)
FAILED.extend(cfail)

print("")
print("=" * 50)
print("總失敗 %d 項" % len(FAILED))
sys.exit(1 if FAILED else 0)
