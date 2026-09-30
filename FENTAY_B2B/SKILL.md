---
name: fentay-b2b
description: 處理豐泰(FENTAY) B2B 布料訂單資料。當用戶要匯入 Excel/PDF 訂單並轉換為 FENTAY_B2B 資料表格式時使用此 Skill。支援豐泰 PDF 訂購單格式，輸出標準 JSON。
---

# 布料訂單匯入處理 - FENTAY B2B

## 用途
當用戶提供 Excel 或 PDF 格式的布料訂單資料，需要轉換為 FENTAY_B2B 資料庫格式時使用此 Skill。

## 支援格式
- Excel (.xlsx, .xls)
- PDF 文件
- CSV 檔案
- 圖片 (PNG, JPG, JPEG) - 透過 OCR 辨識文字

## 工作流程

### Step 1: 讀取來源檔案
- Excel: 使用 xlsx skill 讀取
- PDF: 使用 pdf skill 讀取
- 圖片: 使用視覺模型辨識文字並提取資料

### Step 2: 欄位對照
完整欄位對照表、標題別名、欄位類型、提取與轉換規則，請參考 [MAPPING.md](MAPPING.md)，不在此重複列出。

解析時的核心原則：以標題文字比對為主，與欄位順序無關，不得以第幾欄的位置來對應資料。

### Step 3: 資料驗證
- 檢查必要欄位是否齊全 (ORD_NO, QTY)
- 檢查數值欄位格式 (QTY, PRICE, WIDE)
- 檢查日期欄位格式 (NEED_DATE)
- 確認輸出 data 陣列筆數與輸入資料行數完全一致，不得省略任何一筆

### Step 4: JSON 輸出
參考 [EXAMPLES.md](EXAMPLES.md) 輸出標準 JSON 格式。

## 輸出格式
輸出的 JSON 必須符合以下結構：

```json
{
  "success": true,
  "recordCount": 10,
  "data": [
    {
      "ORD_NO": "6AF1101",
      "MATM_NAME": "3056G",
      "MATM_DESC": "44\"74F黃LJ-A8-P網布/(74F)",
      "WIDE": "44",
      "QTY": 820.0,
      "UNIT": "YD",
      "PRICE": 73.000,
      "NEED_DATE": "20260327",
      "NEED_CUST": "LU1",
      "BRAND_NO": "",
      "CUST_NO": "",
      "CONTACT_NO": ""
    }
  ]
}
```

## 資料完整性要求

**重要：EXAMPLES.md 中的範例資料僅供格式參考，實際處理時必須處理使用者輸入的所有資料！**

輸入資料與輸出 data 陣列必須一一對應，以下情況均屬違規：
- 只處理前幾筆資料
- 只處理 EXAMPLES 中示範的筆數
- 跳過空值或特殊格式的資料列
- 因為資料量大而截斷輸出
- recordCount 數值與 data 陣列長度不一致

**每份訂單文件內的所有資料列，無論數量多少（10筆、38筆、100筆...），都必須全部輸出至 data 陣列。**

## 缺漏欄位處理原則

| 情況 | 處理方式 |
|------|----------|
| 字串欄位在訂單中無對應資料 | 填入空字串 `""` |
| 數值欄位 (NUMBER) 在訂單中無對應資料 | 填入 null |
| 日期欄位在訂單中無對應資料或格式異常 | 字串日期欄位填空字串，DATE 型態欄位填 null |
| 欄位名稱不確定時 | 參考 MAPPING.md 的對照表 |

## 圖片辨識注意事項
- 圖片訂單需支援中文與英文混合格式
- 表格形式的圖片需正確識別行列關係
- 模糊或不清晰的文字需標註 warning
- 辨識結果需經過驗證後輸出
