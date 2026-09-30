#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_env.py - 檢查執行環境：Python 版本、必要套件、輸入檔案、Ollama 與模型。

供 run.bat 選項 6 呼叫，也可獨立執行：
    python check_env.py
"""

import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

REQUIRED_PACKAGES = [("fitz", "PyMuPDF", "PDF 解析（兩條路線都需要）"),
                     ("requests", "requests", "Ollama HTTP 呼叫（僅路線 B）")]
REQUIRED_SKILL_FILES = ("SKILL.md", "MAPPING.md", "EXAMPLES.md")
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
GENERATION_MODELS = ("qwen3.5:4b", "gemma4:12b")
EMBEDDING_HINT = ("bge-m3", "qwen3-embedding")

problems = []


def report(status, label, detail=""):
    mark = "OK  " if status == "ok" else ("WARN" if status == "warn" else "FAIL")
    print("  [%s] %-22s %s" % (mark, label, detail))
    return status


print("=== Python ===")
version = "%d.%d.%d" % sys.version_info[:3]
if sys.version_info >= (3, 10):
    report("ok", "Python version", version)
else:
    report("fail", "Python version", version + " (需要 3.10+)")
    problems.append("Python 版本過舊")

print()
print("=== 必要套件 ===")
for module_name, package_name, purpose in REQUIRED_PACKAGES:
    try:
        __import__(module_name)
        report("ok", package_name, purpose)
    except ImportError:
        report("fail", package_name, "未安裝 - pip install %s" % package_name)
        problems.append("缺少套件 %s" % package_name)

print()
print("=== 輸入檔案 ===")
pdfs = [f for f in sorted(os.listdir(".")) if f.lower().endswith(".pdf")]
if pdfs:
    for name in pdfs:
        report("ok", "PDF", "%s (%.1f KB)" % (name, os.path.getsize(name) / 1024))
else:
    report("fail", "PDF", "找不到 *.pdf")
    problems.append("資料夾內沒有 PDF")

print()
print("=== Skill 規格檔 ===")
skill_dir = "FENTAY_B2B"
if os.path.isdir(skill_dir):
    for name in REQUIRED_SKILL_FILES:
        path = os.path.join(skill_dir, name)
        if os.path.isfile(path):
            report("ok", name, "%.1f KB" % (os.path.getsize(path) / 1024))
        else:
            report("fail", name, "缺少")
            problems.append("缺少 %s\\%s" % (skill_dir, name))
else:
    report("fail", "FENTAY_B2B", "目錄不存在")
    problems.append("FENTAY_B2B 目錄不存在")

print()
print("=== 專案程式 ===")
for name in ("fentay_common.py", "ollama_fentay.py", "pdf_text_fentay.py",
             "test_fentay.py"):
    if os.path.isfile(name):
        report("ok", name, "%.1f KB" % (os.path.getsize(name) / 1024))
    else:
        report("fail", name, "缺少")
        problems.append("缺少 %s" % name)

print()
print("=== Ollama 與模型 ===")
try:
    with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=5) as response:
        import json
        names = [m["name"] for m in json.load(response).get("models", [])]
    report("ok", "Ollama service", "localhost:11434 可連線")
except (urllib.error.URLError, OSError) as exc:
    report("warn", "Ollama service", "無法連線 (%s) - 路線 C 仍可使用" % exc)
    names = []

generation = [n for n in names if not any(h in n for h in EMBEDDING_HINT)]
embedding = [n for n in names if any(h in n for h in EMBEDDING_HINT)]

for name in GENERATION_MODELS:
    if any(name.split(":")[0] in n for n in generation):
        report("ok", name, "已下載")
    else:
        report("warn", name, "未下載 - ollama pull %s" % name)

if embedding:
    report("warn", "embedding models", "%s（僅供向量化，不能做生成）"
           % ", ".join(embedding))

if not generation:
    report("fail", "generating model", "沒有可用的生成模型，路線 B 無法執行")

print()
print("=== 結論 ===")
if problems:
    print("  發現 %d 個問題：" % len(problems))
    for item in problems:
        print("    - %s" % item)
    sys.exit(1)
print("  環境檢查通過，路線 C 可直接執行。")
if generation:
    print("  路線 B 需要其中一個生成模型：%s" % ", ".join(GENERATION_MODELS[:1]))
sys.exit(0)
