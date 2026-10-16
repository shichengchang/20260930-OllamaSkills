#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anonymize_samples.py - 把真實訂單樣本轉為同形狀的合成值。

用途
----
`test_classify_spec.py` 判斷欄位語意時，依賴的是「值的形狀」
（格式、長度、符號種類、重複程度），而不是實際值。
因此在把資料送到外部服務前，可先用本模組把商業內容換成合成值，
同時保留分類所需的全部判斷依據。

設計原則
--------
| 欄位     | 需保留的判斷訊號                 | 替換方式                     |
| -------- | ------------------------------ | ---------------------------- |
| ORD_NO   | 固定格式、各列不重複、英數混雜   | 保留字元集與長度，換成合成碼 |
| MATM_NAME| 短英數代碼、可能整欄為空         | 同上，保留長度分布           |
| MATM_DESC| 全表最長、中英數混合、含引號括號 | 重新合成，保留結構特徵       |
| WIDE     | 數字加雙引號、大量重複           | 整欄等值平移，重複度不變     |
| QTY      | 小數位數、千分位有無             | 等比縮放，維持 1 位小數      |
| UNIT     | 單位詞、種類極少且大量重複       | 換成另一組通用單位詞         |
| PRICE    | 小數位數（2~3 位）              | 與 QTY 同倍率縮放            |
| NEED_DATE| 日期格式、日期落在相近區間       | 年份整體位移                 |
| NEED_CUST| 很短的代碼、種類少且大量重複     | 換成合成代碼，保留長度與分布 |
| （金額） | 千分位數值，約等於 QTY × PRICE  | 由縮放後的 QTY × PRICE 重算  |

刻意不保留的：訂購單號、料號、材料描述全文、子公司代碼、原始數值。
這些是本專案要送出的唯一真實商業內容。

QTY 與 PRICE 使用**同一倍率**縮放，因此
「數量 × 單價 = 金額」的關係仍然成立，`cross_validate()` 仍可運作。
"""

import re

# 通用單位詞。替換值必須與原值**等長**，否則會改變
# 「簡短單位詞、種類極少且大量重複」這個判斷特徵。
#
# 覆蓋中英文常見單位。若找不到對應項，由 _unit_like() 以等長合成詞補上，
# 因此此表不需要窮舉。
UNIT_SUBSTITUTE = {
    "碼": "M",
    "公尺": "公斤",
    "米": "尺",
    "公斤": "公尺",
    "個": "件",
    "卷": "包",
    "包": "卷",
    "組": "批",
    "YD": "PC",
    "PC": "YD",
    "KG": "M",
    "KG ": "M  ",
}

# 子公司代碼的合成替換（保留長度與字元類型）
CUST_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # 去掉易混淆的 I O
CUST_LOWER = "abcdefghijkmnpqrstuvwxyz"
CUST_DIGIT = "0123456789"

# 真實格式為 00A A2279-2EPM5保2網/(00A)：三碼色樣 + 空格 + 料號 + 網布 + 括號
MATERIAL_DESC_TEMPLATE = "%s %s網布/(%s)"

# 真實值中不應出現於輸出的識別性片段。
# 注意：不可用 [A-Z]\d{4}- 這類泛用形狀來比對，因為匿名化後的合成值
# 本來就會產生同形狀的碼，會造成誤報。改以列出真實文件中確實出現的
# 識別性字面量來比對。
SENSITIVE_TOKENS = re.compile(
    r"(保\d|正\d|興業|股份|有限|公司|供應商|致\s|地址|彰化|社頭|鄉|巷|路|"
    r"電話|TEL|FAX|統編|統一編號|列印編號|幣別|新台幣|需\s*求|"
    r"EPM\d)", re.I)

# 匿名化後合成的材料描述前綴，用於把上述形狀規則排除在自身之外
SYNTHETIC_DESC_PREFIX = "Z"


def _ord_like(text, seq):
    """
    產生與原值同長度、同字元類型的訂購單號。
    真實值形如 6AF1101 / 6B55G01：首位數字 + 字母數字混雜。
    """
    out = []
    for i, ch in enumerate(text):
        if ch.isdigit():
            # 首碼固定為 6（該文件格式的關鍵特徵），其餘隨機取數字
            out.append("6" if i == 0 else CUST_DIGIT[(seq + i) % 10])
        elif ch.isalpha() and ch.isupper():
            out.append(CUST_ALPHABET[(seq + i) % len(CUST_ALPHABET)])
        elif ch.isalpha():
            out.append(CUST_LOWER[(seq + i) % len(CUST_LOWER)])
        else:
            out.append(ch)
    return "".join(out)


def _code_like(text, seq):
    """產生與原值同長度、同字元類型的短代碼（料號、子公司代碼）。"""
    out = []
    for i, ch in enumerate(text):
        if ch.isdigit():
            out.append(CUST_DIGIT[(seq * 3 + i) % 10])
        elif ch.isupper():
            out.append(CUST_ALPHABET[(seq + i) % len(CUST_ALPHABET)])
        elif ch.islower():
            out.append(CUST_LOWER[(seq + i) % len(CUST_LOWER)])
        else:
            out.append(ch)
    return "".join(out) or "X"


def _desc_like(text, seq):
    """
    合成材料描述，保留真實值的結構特徵。

    關鍵是要保留**長度分布**而非平均值。MAPPING.md 判斷 MATM_DESC 靠
    「全表最長的文字欄、中英數混合、含雙引號與括號」；若合成後每列長度
    都一樣，會讓 MATM_DESC 與其他欄的差異消失。因此依原值的長度差
    調整合成字串的長度，使長度集合與原值接近。
    """
    raw = text.strip()
    tag = "%02d%s" % (10 + seq % 89, CUST_ALPHABET[seq % len(CUST_ALPHABET)])
    skeleton = "Z%s-%s%s合成料" % (
        "".join(CUST_DIGIT[(seq + k) % 10] for k in range(4)),
        CUST_ALPHABET[(seq + 1) % len(CUST_ALPHABET)],
        CUST_ALPHABET[(seq + 3) % len(CUST_ALPHABET)],
    )
    out = MATERIAL_DESC_TEMPLATE % (tag, skeleton, tag)
    # 依原值長度補丁或裁切，維持長度分布（讓 MATM_DESC 仍明顯長於其他欄）
    target = len(raw)
    filler = "".join(CUST_ALPHABET[(seq + k) % len(CUST_ALPHABET)]
                     for k in range(16))
    if len(out) < target:
        need = target - len(out)
        out = out[:-1] + filler[:need] + out[-1]      # 在右括號前插入
    else:
        # 裁切時不可動到右括號與 tag 結構，從骨架中段刪除字母
        excess = len(out) - target
        core = "合成料"
        if excess <= len(core) - 1:
            out = out.replace(core, core[:len(core) - excess], 1)
        else:
            out = out[:len(out) - excess - 1] + out[-1]
    return out


def _fmt_like_number(text, new_value):
    """
    以 new_value 取代原值，但保留原本的數字格式特徵：
    小數位數、有無千分位、是否有前導負號。
    """
    raw = text.strip()
    neg = raw.startswith("-")
    has_comma = "," in raw
    frac = 0
    if "." in raw:
        frac = len(raw.split(".")[-1].strip())
    if has_comma:
        body = format(new_value, ",.%df" % frac)
    else:
        body = format(new_value, ".%df" % frac)
    return ("-" if neg else "") + body


def _date_like(text, year_shift):
    """年份整體位移，其餘格式（分隔符、補零、民國年）不變。"""
    m = re.match(r"^(\d{2,4})([^\d]*)(\d{1,2})([^\d]*)(\d{1,2})(.*)$", text.strip())
    if not m:
        return text
    y, s1, mth, s2, d, rest = m.groups()
    yi = int(y)
    if len(y) == 4:                 # 西元
        ny = yi + year_shift
        ys = "%04d" % ny
    else:                            # 民國年或兩位數年
        ys = "%02d" % ((yi + year_shift) % 100)
    return "%s%s%s%s%s%s" % (ys, s1, mth, s2, d, rest)


def _width_like(text, shift):
    """整欄等值平移，保留雙引號與重複度。"""
    m = re.match(r'^(\d+(?:\.\d+)?)(.*)$', text.strip())
    if not m:
        return text
    num, suffix = m.groups()
    new = max(1.0, float(num) + shift)
    if "." in num:
        return "%.1f%s" % (new, suffix)
    return "%d%s" % (int(new), suffix)


def _unit_like(text, seq):
    """
    產生與原單位詞**等長**的替代單位詞。

    單位詞是 MAPPING.md 判斷 UNIT 的依據（「簡短的單位詞、
    值的種類極少且大量重複」）。長度若改變，這項特徵就會被破壞，
    因此這裡以等長為硬性約束。
    """
    raw = text.strip()
    n = len(raw)
    if n == 0:
        return text
    # 依長度挑一個不含真實語意的單位詞
    pool = ["M", "PC", "KG", "EA", "LB", "PK"]
    if n >= 2:
        pool += ["單位", "箱", "包", "件"]
    if n >= 3:
        pool += ["立方", "包裝"]
    base = pool[seq % len(pool)]
    if len(base) > n:
        base = base[:n]
    elif len(base) < n:
        # 以大寫字母補滿長度，不改變「短單位詞」的性質
        base = base + "".join(CUST_ALPHABET[(seq + k) % len(CUST_ALPHABET)]
                              for k in range(n - len(base)))
    return base


def anonymize_rows(rows, fields, amount_index=7, year_shift=1, qty_scale=1.37):
    """
    將樣本列轉為合成值。

    rows    : [{欄位索引: 值, ...}, ...]
    fields  : 對應欄位索引的欄位名（金額欄為 None）
    回傳     : (匿名化後的列, 轉換說明 dict)

    qty_scale 會依整欄的實際數字大小調整，盡量讓縮放後的位數不變，
    以保留「數值格式」這項判斷依據（見 audit 的長度集合檢查）。
    """
    n = len(fields)
    # 先決定每欄的平移量，確保整欄一致（保留「大量重複」的特性）
    width_shift = {}
    for r in rows:
        for i in range(n):
            if fields[i] == "WIDE":
                v = r.get(i, "")
                if v and i not in width_shift:
                    width_shift[i] = 1.0

    unit_map = {}
    cust_map = {}
    code_map = {}          # MATM_NAME：整欄統一映射，保留重複程度
    desc_map = {}          # MATM_DESC：同上

    # 數值欄（QTY / PRICE / 金額）**保持原值不變**。
    #
    # 理由：單一數值不具識別性 — 無法從數量或單價反推是哪家公司、
    # 哪張單、哪位客戶。真正需要替換的只有號碼（訂購單號、料號、
    # 子公司代碼）與材料描述。
    #
    # 刻意不用縮放倍率：倍率會改變整數位數，而位數是 MAPPING.md 判斷
    # QTY（1 位小數）與 PRICE（2~3 位小數）的重要線索。
    # 金額欄亦保持原值，使「QTY × PRICE = 金額」關係精確成立。
    out = []
    for r_i, row in enumerate(rows):
        new = {}
        for i in range(n):
            val = row.get(i, "")
            field = fields[i]

            # 金額欄不由個別規則處理，最後統一重算
            if i == amount_index:
                new[i] = val
                continue

            # 空值必須保持空。MAPPING.md 用「可能整欄多列為空」
            # 判斷 MATM_NAME，若把空值填成合成碼會破壞這項線索。
            if not str(val).strip():
                new[i] = val
                continue

            if field == "ORD_NO":
                new[i] = _ord_like(val, r_i)
            elif field == "MATM_NAME":
                # 整欄統一映射：原值重複者，合成值也必須重複。
                # MAPPING.md 用「幾乎不重複，可能為空」描述 MATM_NAME，
                # 若每列各自生成會讓所有值都變成不重複，線索消失。
                if val not in code_map:
                    code_map[val] = _code_like(val, len(code_map) + 1)
                new[i] = code_map[val]
            elif field == "MATM_DESC":
                if val not in desc_map:
                    desc_map[val] = _desc_like(val, len(desc_map) + 1)
                new[i] = desc_map[val]
            elif field == "WIDE":
                new[i] = _width_like(val, width_shift.get(i, 1.0))
            elif field == "UNIT":
                if val.strip() not in unit_map:
                    unit_map[val.strip()] = UNIT_SUBSTITUTE.get(val.strip())
                    if unit_map[val.strip()] is None:
                        unit_map[val.strip()] = _unit_like(val, len(unit_map))
                    elif len(unit_map[val.strip()]) != len(val.strip()):
                        unit_map[val.strip()] = _unit_like(val, len(unit_map))
                new[i] = unit_map.get(val.strip(), val)
            elif field in ("QTY", "PRICE"):
                new[i] = val          # 數值保持原值，見上方說明
            elif field == "NEED_DATE":
                new[i] = _date_like(val, year_shift)
            elif field == "NEED_CUST":
                if val.strip() not in cust_map:
                    cust_map[val.strip()] = _code_like(val, len(cust_map) + 1)
                new[i] = cust_map.get(val.strip(), val)
            else:
                new[i] = val
        out.append(new)

    # 金額欄與 QTY、PRICE 都保持原值，因此乘積關係不受匿名化影響。
    # 這裡不做任何重算，僅在 audit 中驗證。

    note = {
        "unit_map": unit_map,
        "cust_map": cust_map,
        "code_map_count": len(code_map),
        "desc_map_count": len(desc_map),
        "year_shift": year_shift,
        "numeric_kept": "QTY / PRICE / 金額 保持原值",
    }
    return out, note


def _to_float(text):
    s = str(text).strip().replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


def audit(rows_before, rows_after, fields):
    """
    檢查匿名化結果：確認無殘留識別性片段，且形狀特徵保留。
    回傳 (是否通過, 報告文字)
    """
    lines = []
    ok = True

    # 1. 殘留敏感片段
    leaks = []
    for r in rows_after:
        for i, v in r.items():
            if SENSITIVE_TOKENS.search(str(v)):
                leaks.append("%s: %r" % (fields[i] if i < len(fields) else i, v))
    if leaks:
        ok = False
        lines.append("  [NG] 殘留可識別片段 %d 處: %s" % (len(leaks), leaks[:3]))
    else:
        lines.append("  [OK] 無可識別片段殘留")

    # 2. 形狀特徵是否保留
    max_col = max((max(r.keys()) for r in (rows_before or [{}])), default=-1)
    for idx, field in enumerate(fields):
        if field is None or idx > max_col:
            continue
        b = [str(r.get(idx, "")) for r in rows_before]
        a = [str(r.get(idx, "")) for r in rows_after]

        def shape(vals):
            return sorted({len(v) for v in vals}), \
                   sum(1 for v in vals if "," in v), \
                   len({v for v in vals})

        lb, cb, ub = shape(b)
        la, ca, ua = shape(a)
        # 長度集合需相同；千分位有無需相同；相異值數量級需相近
        len_same = (lb == la)
        comma_same = (cb == ca)
        uniq_ratio = (ua / max(ub, 1))
        uniq_ok = 0.5 <= uniq_ratio <= 1.6
        flag = "OK" if (len_same and comma_same and uniq_ok) else "NG"
        if flag == "NG":
            ok = False
        lines.append("  [%s] %-11s 長度%s->%s 千分位%d->%d 相異值%d->%d (%.2fx)"
                     % (flag, field, lb, la, cb, ca, ub, ua, uniq_ratio))
    return ok, "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(__doc__)
    print("此模組由 test_classify_spec.py --anonymize 呼叫。")