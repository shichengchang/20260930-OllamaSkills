# FENTAY B2B 訂單解析工具

將豐泰（FENTAY）訂購單 PDF 轉換為 `FENTAY_B2B` 資料表格式的 JSON。

本專案實作兩條解析路線並提供量化比對，用來評估「要不要用 LLM 來做這件事」。

---

## 快速開始

```
雙擊 run.bat  →  輸入數字選擇功能
```

或直接執行：

```powershell
python test_fentay.py                                                  # 單元測試
python pdf_text_fentay.py --pdf ".\豐泰.pdf" --compare ".\expected.json"   # 路線 C
```

---

## 檔案結構

```
fentay_common.py       規則的唯一實作（MAPPING.md 轉換規則 + 輸出契約）
├── ollama_fentay.py   路線 B：PDF → PNG → vision 模型 → JSON
└── pdf_text_fentay.py 路線 C：PDF 文字層 → 座標分組 → JSON

test_fentay.py         89 項單元測試
compare_results.py     批次比較所有 result.*.json
check_env.py           環境檢查
expected.json          38 筆基準答案
run.bat                功能選單（雙擊執行）
FENTAY_B2B/            Skill 規格（SKILL.md / MAPPING.md / EXAMPLES.md）
```

`fentay_common.py` 是關鍵設計：轉換規則只有一份實作，兩條路線共用，
因此比對結果才具有可比性。它只依賴 Python 標準函式庫，不需要 `fitz` 或 `requests`。

---

## 兩條解析路線

|          | 路線 B（vision） | 路線 C（文字層） |
| -------- | ---------------- | ---------------- |
| 原理     | PDF 渲染成 PNG 餵給 vision 模型 | 直接讀 PDF 文字層 |
| 耗時     | 約 115 秒 / 2 頁 | **約 0.02 秒 / 2 頁** |
| 外部依賴 | Ollama（16 GB 模型常駐） | 無 |
| 掃描件   | 可處理 | **失效**（會明確報錯） |
| 版面容忍 | 較高 | 較低（依賴表頭虛線定位欄位） |

### 兩種工作模式：`pure` 與 `hybrid`

路線 B 有兩種模式，差別在於 **MAPPING.md 的轉換規則由誰執行**。

```
pure 模式（全部交給模型）
  PDF ──渲染──> PNG ──> vision 模型 ──讀圖 + 套用全部規則──> 最終 JSON
                                        │
                                        └─ 單位 碼→YD
                                           日期 2026/03/27→20260327
                                           金額 略過
                                           BRAND_NO 等三欄補空字串

hybrid 模式（模型只讀表，規則交給程式）
  PDF ──渲染──> PNG ──> vision 模型 ──只讀表格──> 原始值 + 原始欄位名
                                        │
                     ┌──────────────────┘
                     ▼
              fentay_common.py ──套用同一份規則──> 最終 JSON
```

`pure` 模式的用途是**測試 Skill 本身的指令是否有效** — 把 `SKILL.md`、
`MAPPING.md`、`EXAMPLES.md` 全文塞進 prompt，看模型能不能照做。
這是評估「這個 Skill 寫得好不好」的方法。

`hybrid` 模式則是**實務做法** — 模型只負責它擅長的讀圖，
規則交給確定性的程式碼執行。

### 實測結果（`豐泰.pdf`，2 頁共 38 筆）

| 組合 | 筆數 | 欄位正確率 | 耗時 |
| ---- | ---- | ---------- | ---- |
| 路線 C | 38/38 | 100.0% | 0.02 秒 |
| 路線 B `qwen3.5:4b` hybrid | 38/38 | **99.8%** | 116 秒 |
| 路線 B `qwen3.5:4b` pure | 38/38 | 89.9% | 122 秒 |
| 路線 B `gemma4:12b` hybrid | 35 | 75.5% | 441 秒 |
| 路線 B `gemma4:12b` pure | 36 | 73.6% | 505 秒 |

輸入法為 `temperature=0`，但小模型在視覺辨識上仍非確定性輸出，
同一組合重跑的正確率會有數個百分點波動。上表為最近一次執行結果。

#### hybrid 模式剩下的 1 處錯誤

```
6AF1122  預期='44" 黑保利2 CDP布/(00A)'  實際='44"黑保利2 CDP布/(00A)'
```

材料名稱中漏掉一個空格。原文的空格本身沒規律（38 筆中 3 筆在寬度後有空格、
28 筆沒有），因此**沒有任何內部一致性訊號可用來判斷空格是否遺失**，
只能靠 OCR 本身。目前無解。

---

## OCR 失真修正：色碼 O/0 混淆

vision 模型在低解析度下會把數字 `0` 認成字母 `O`：

```
6AF1105  0AVLJA4497K9EPM5回網/(0AV)  →  OAVLJA4497K9EPM5回網/(0AV)
6AF1112  0BG A2279-2EPM5保利2/(0BG)  →  OBG A2279-2EPM5保利2/(0BG)
```

**成因**：豐泰訂單為 8pt 中文字，200 DPI 下字高僅約 22px，
在 MingLiU 明體字型中數字 `0`（窄橢圓）與字母 `O`（方圓）幾乎無法區分。

**解法**：`fentay_common.py` 的 `fix_ocr_color_code()` 自我一致性檢查。
豐泰的材料名稱常以色碼開頭，且尾端括號會重複該色碼：

```
0AVLJA4497K9EPM5回網/(0AV)
└─ 前綴 ─┘      └ 括號 ┘
```

當兩者只有首字元 `O`/`0` 之差時，以括號內的數字 `0` 為準修正。
規則嚴格（要求後兩個字母完全相同），對正常資料為 no-op。

實測效果：3/3 執行皆修正 2 筆，正確率從 99.3% 提升至 **99.8%**。
對 `expected.json`、路線 C 結果、`gemma4` 結果皆為 0 影響。

---

## 關於 DPI：不要盲目提高

實測發現 **Ollama 服務端對視覺 token 數有上限**（約 4,049 tokens，
即約 4,150,000 px）。超過上限的圖片會被降解析度，因此
**提高 DPI 不等於提高模型看到的清晰度**：

| 設定 DPI | 實際像素 | 視覺 token | 是否被壓縮 | **實際有效 DPI** |
| --- | --- | --- | --- | --- |
| 150 | 1.59M | 1,577 | 否 | 150 |
| **200（預設）** | 2.82M | 2,773 | 否 | **200** |
| 300 | 6.34M | 4,049 | 是 | ~196 |
| 400 | 11.27M | 4,049 | 是 | **~148（比 200 更差）** |

實測（`qwen3.5:4b` hybrid，各跑 3 次）：

| DPI | MATM_DESC 錯誤 | 整體 | 備註 |
| --- | --- | --- | --- |
| **200** | 1 / 1 / 1 | **99.8%** | 穩定 |
| 400 | 2 / 2 / 2 | 99.6% | 反而變差 |
| 240 | 1 + 5 次失敗 | — | Ollama 行程崩潰（記憶體耗盡） |

400 DPI 變差的原因是：模型把 `0BG` 的**兩處**都讀成 `OBG`，
內部一致，修正規則偵測不到。

**300 DPI 以上在 31.5 GB RAM 的機器上曾導致 Ollama 行程崩潰。**
若要提高 DPI，請先確認記憶體充足並觀察穩定性。

#### `pure` vs `hybrid` 的差距來自哪裡

以 `qwen3.5:4b` 為例，`hybrid` 比 `pure` 高 9.4 個百分點，逐欄看：

| 欄位 | pure | hybrid |
| ---- | ---- | ------ |
| `NEED_DATE` | **0.0%** | 100.0% |
| `PRICE` | 92.1% | 100.0% |
| `BRAND_NO` / `CUST_NO` / `CONTACT_NO` | **整欄消失** | 100.0% |

- **`NEED_DATE`**：38/38 筆原樣輸出 `2026/03/27`，完全沒套用「去除斜線」規則
- **`BRAND_NO` 等三欄**：38 筆全部沒有這三個欄位，觸發 38 個驗證問題。
  SKILL.md 的輸出格式範例有列出，模型還是漏掉
- **`PRICE`**：3 筆把 `9.30` 誤讀成 `9.33`

**結論：寫在 prompt 裡的規則對 4B 模型不可靠。**
只要規則是明確的轉換（單位、日期、小數位、預設值），
就應該實作在程式碼裡，不要交給模型。

#### 路線 B vs 路線 C 的差距

加上 OCR 修正規則後只剩 **1 格**，全部在 `MATM_DESC`：

```
6AF1122  44" 黑保利2 CDP布/(00A)   vs  44"黑保利2 CDP布/(00A)
```

修正規則啟用前是 3 格，其中 2 格為數字 `0` 被認成字母 `O`（已修正）。
剩下這 1 格是空格遺失，**無解** — 原文空格本身沒規律，沒有內部訊號可判斷。

這類失真發生在讀圖階段，規則套用是在之後，因此
**再增加多少轉換規則都無法彌補**。要根治只能提升 OCR 品質，
但實測提高 DPI 反而更差（見「關於 DPI」章節）。

### 建議

**若 PDF 都有文字層，以路線 C 為主力，路線 B 留給掃描件。**
路線 C 在速度與準確度上都勝出，且不依賴任何模型。

---

## 關於 `expected.json` 的循環比對

`expected.json` 是用**路線 C 的方法**產生的，所以路線 C 與它比對必然是 100%，
這**不能**作為路線 C 正確的證據。

因此 C 額外提供獨立的交叉驗證：

```
QTY x PRICE ≈ 該列的「金額」
```

此檢查不依賴 `expected.json`。若欄位歸屬錯位，QTY 與 PRICE 會對到不同資料列，
相乘就不會等於該列金額，因此能有效偵測錯位。實測 38/38 通過。

路線 B 與 `expected.json` 的比對則是有效的，因為兩者走完全不同的擷取路徑。

---

## 轉換規則摘要

實作於 `fentay_common.py`，對應 `FENTAY_B2B/MAPPING.md`：

| 欄位 | 來源欄位 | 規則 |
| ---- | -------- | ---- |
| `ORD_NO` | 訂購單號 | 直接填入 |
| `MATM_NAME` | 料號 | 直接填入 |
| `MATM_DESC` | 材料名稱/顏色代碼 | **保留原始字串（含雙引號）** |
| `WIDE` | 規格 | **去除雙引號僅留數字**（`44"` → `44`） |
| `QTY` | 數量 | 去千分位，小數 2 位 |
| `UNIT` | 單位 | `碼`→`YD`、`公尺`→`M`、`公斤`→`KG`、`個`→`PC` |
| `PRICE` | 單價 | 去千分位，小數 3 位 |
| `NEED_DATE` | 交貨日期 | 轉 `YYYYMMDD`，民國年 +1911 |
| `NEED_CUST` | 需求子公司 | 直接填入 |
| `BRAND_NO` / `CUST_NO` / `CONTACT_NO` | — | 固定空字串 |
| （不匯入） | 金額 | 略過 |

`WIDE` 去引號但 `MATM_DESC` 保留引號，這兩者行為不同，
是刻意的設計而非不一致。兩者皆有單元測試鎖定。

---

## 執行方式

### 功能選單

```
run.bat
```

| 選項 | 功能 | 耗時 |
| ---- | ---- | ---- |
| 1 | 單元測試（89 項） | 約 2 秒 |
| 2 | 路線 C：文字層解析（無需 Ollama） | 約 1 秒 |
| 3 | 路線 B：`qwen3.5:4b` + hybrid（推薦） | 約 2 分鐘 |
| 4 | 路線 B：完整矩陣（2 模型 × 2 模式） | 約 20 分鐘 |
| 5 | 比較所有既有結果 | 立即 |
| 6 | 環境檢查（Python / 套件 / Ollama / 模型） | 立即 |
| 0 | 離開 | — |

選項 4 執行前會要求確認。選項 6 會自動略過 embedding 模型
（`bge-m3`、`qwen3-embedding`），它們不能做生成。

### 直接執行

```powershell
# 路線 C
python pdf_text_fentay.py --pdf ".\豐泰.pdf" --compare ".\expected.json"

# 路線 B（hybrid：規則由 Python 套用）
python ollama_fentay.py --pdf ".\豐泰.pdf" --skill-dir ".\FENTAY_B2B" `
  --model qwen3.5:4b --mode hybrid --compare ".\expected.json"

# 路線 B（pure：規則交給模型執行，用於測試 Skill 指令是否有效）
python ollama_fentay.py --pdf ".\豐泰.pdf" --skill-dir ".\FENTAY_B2B" `
  --model qwen3.5:4b --mode pure --compare ".\expected.json"
```

---

## 輸出

所有結果集中寫入 `output/`，專案根目錄保持乾淨。

```
output/
├── result.C.json                                  ← 固定檔名，每次覆蓋
├── result.C.20260930-145731.report.txt            ← 帶時間戳，保留歷次紀錄
├── result.qwen3.5_4b.hybrid.json
├── result.qwen3.5_4b.hybrid.20260930-145945.report.txt
├── result.qwen3.5_4b.hybrid.20260930-151811.report.txt
└── ...
```

**JSON 與報告的差異**

| | JSON | 報告 `.report.txt` |
|---|---|---|
| 檔名 | 固定（每次覆蓋） | 帶時間戳 `YYYYMMDD-HHMMSS` |
| 用途 | 下游取用最新結果 | 保留每次執行紀錄 |
| 比對 | `compare_results.py` 只讀最新 | 不參與比對 |

報告檔名帶時間戳，因此 `output/` 會隨執行次數累積報告檔。
需要清理舊報告時可直接刪除帶時間戳的 `.report.txt`，JSON 不受影響。

每個 JSON 都附帶 `_meta` 區塊記錄來源與時間：

```json
{
  "success": true,
  "recordCount": 38,
  "data": [ ... ],
  "_meta": {
    "generatedAt": "2026-09-30T14:25:40+08:00",
    "startedAt": "2026-09-30T14:23:44+08:00",
    "elapsedSec": 116.472,
    "source": {
      "route": "B",
      "mode": "hybrid",
      "model": "qwen3.5:4b",
      "pdf": "豐泰.pdf",
      "dpi": 200,
      "pages": 2
    }
  }
}
```

時間為台灣時區（UTC+8）。`_meta` 放在獨立區塊而非頂層，
是為了不影響既有的 `{success, recordCount, data}` 結構，
下游取用 `data` 的程式碼無需修改。

報告檔（`.report.txt`）開頭也記錄產出時間、開始時間與耗時。
檔名格式為 `result.<來源>.<YYYYMMDD-HHMMSS>.report.txt`。

用 `--out-dir` 可改變輸出位置；`pdf_text_fentay.py --report` 可指定報告檔路徑。

### 常用參數

`ollama_fentay.py`

| 參數 | 說明 |
| ---- | ---- |
| `--model` | 可重複指定以比較多個模型 |
| `--mode` | `pure` / `hybrid`，可重複指定 |
| `--dpi` | 預設 200。提高不一定更好，見「關於 DPI」章節 |
| `--rows-per-call N` | 每頁切 N 段分次呼叫，用於對抗輸出截斷 |
| `--max-pages N` | 只處理前 N 頁 |
| `--json-format none` | 關閉 Ollama 的 JSON 約束 |

`pdf_text_fentay.py`

| 參數 | 說明 |
| ---- | ---- |
| `--dump-columns` | 輸出欄位偵測診斷（換版式時先跑這個） |
| `--show-diffs N` | 差異明細顯示筆數 |

---

## 環境需求

| 項目 | 需求 |
| ---- | ---- |
| Python | 3.10+ |
| 套件 | `PyMuPDF`、`requests` |
| Ollama | 僅路線 B 需要 |
| 模型 | `qwen3.5:4b`（建議）、`gemma4:12b` |

```powershell
pip install PyMuPDF requests
```

不使用 `ollama` Python 套件，直接以 HTTP 呼叫 API。

---

## 已知限制

1. **路線 C 依賴表頭虛線定位欄位。** 換成框線、無線、或數字欄位數變多時可能失效。
   程式有備援機制（L2 表頭文字、L3 資料分群）並會發出警告，但備援的欄位語意
   判斷正確性未經實測。換供應商版式時請先跑 `--dump-columns` 確認。
2. **僅測過這一份 PDF、這個供應商、繁體中文。** 跨頁表格、無框線、民國日期
   等情境未測試。
3. **`gemma4:12b` 不建議用於本任務。** 參數量比 `qwen3.5:4b` 大三倍，
   但欄位正確率低 24 個百分點，且會幻覺出不存在的訂單號。
4. **`pure` 模式會違反 Skill 規則。** 實測 `NEED_DATE` 正確率為 0%
   （未轉換格式）、`BRAND_NO`/`CUST_NO`/`CONTACT_NO` 整欄消失。
   規則寫在 prompt 裡對小模型不可靠，應實作在程式碼中（用 `hybrid`）。
5. **`PRICE` 的小數位在 JSON 中不會保留。** `73.000` 會變成 `73.0`，
   JSON number 無位數語意。若下游需要 `NUMBER(10,3)` 的固定補位需另行處理。

---

## 已知問題

`.gitignore` 目前被標記為 modified（新增 `*.json` 與 `*.txt`），
這不是本專案程式碼變更的一部分，若非預期請自行檢查。
