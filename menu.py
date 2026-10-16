#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
menu.py - 功能選單（run.bat 僅負責啟動本檔）。

選項定義集中在 ACTIONS 一張表，新增解析路線只要加一列，
不必再到 .bat 同步選單文字、選項範圍與分支。

選項的前置條件是「宣告式」的：
  needs_pdf=True   需要目錄內有 *.pdf
  requires=[...]   需要這些檔案存在（例如路線 B 需要 Skill 規格檔）
只有不符合條件的選項會被擋下，不會影響其他選項。

用法：
    python menu.py          # 互動選單（run.bat 呼叫的就是這個）
    python menu.py --list   # 只印出選項表，不進入互動
"""

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# argv 中代表 PDF 路徑的佔位符，執行前才替換
PDF_PLACEHOLDER = "{pdf}"


class Action:
    """一個選項。argv 為完整參數串列（不含直譯器本身）。"""

    def __init__(self, key, title, note="", argv=None, needs_pdf=True,
                 requires=(), confirm=None, exit=False):
        self.key = key
        self.title = title
        self.note = note
        self.argv = list(argv or [])
        self.needs_pdf = needs_pdf
        self.requires = list(requires)
        self.confirm = confirm
        self.exit = exit

    @property
    def script(self):
        return self.argv[0] if self.argv else ""

    def label(self):
        note = "  (%s)" % self.note if self.note else ""
        return "%s) %s%s" % (self.key, self.title, note)


ACTIONS = [
    Action("1", "單元測試", "約 2 秒",
           argv=["test_fentay.py"], needs_pdf=False),

    Action("2", "路線 C：文字層解析", "約 1 秒，無需 Ollama",
           argv=["pdf_text_fentay.py", "--pdf", PDF_PLACEHOLDER,
                 "--compare", "expected.json", "--dump-columns"]),

    Action("3", "路線 B：qwen3.5:4b hybrid", "推薦，約 2 分鐘",
           argv=["ollama_fentay.py", "--pdf", PDF_PLACEHOLDER,
                 "--skill-dir", "FENTAY_B2B", "--model", "qwen3.5:4b",
                 "--mode", "hybrid", "--dpi", "200",
                 "--out-base", "result", "--compare", "expected.json"],
           requires=["FENTAY_B2B/SKILL.md"]),

    Action("4", "路線 B：完整矩陣", "2 模型 × 2 模式，約 20 分鐘",
           argv=["ollama_fentay.py", "--pdf", PDF_PLACEHOLDER,
                 "--skill-dir", "FENTAY_B2B",
                 "--model", "qwen3.5:4b", "--model", "gemma4:12b",
                 "--mode", "pure", "--mode", "hybrid", "--dpi", "200",
                 "--out-base", "result", "--compare", "expected.json",
                 "--show-diffs", "12"],
           requires=["FENTAY_B2B/SKILL.md"],
           confirm="完整矩陣約需 20 分鐘，確定繼續？(y/N)"),

    Action("5", "比較所有既有結果", "立即",
           argv=["compare_results.py"], needs_pdf=False),

    Action("6", "環境檢查", "立即",
           argv=["check_env.py"], needs_pdf=False),

    Action("7", "路線 D：文字層 + LLM 判斷欄位語意", "約 5 秒",
           argv=["pdf_text_llm_fentay.py", "--pdf", PDF_PLACEHOLDER,
                 "--model", "qwen3.5:4b", "--samples", "10", "--runs", "1",
                 "--out-base", "result", "--compare", "expected.json",
                 "--dump-columns"]),

    Action("0", "離開", needs_pdf=False, exit=True),
]


def find_pdf(directory=HERE):
    """回傳目錄內第一份 PDF；以排序確保結果可重現。"""
    matches = sorted(glob.glob(os.path.join(directory, "*.pdf")))
    return os.path.basename(matches[0]) if matches else None


def missing_requirements(action, directory=HERE):
    """回傳此選項缺少的前置檔案清單。"""
    return [name for name in action.requires
            if not os.path.exists(os.path.join(directory, name))]


def build_argv(action, pdf, directory=HERE):
    """把 argv 中的 PDF 佔位符換成實際檔名。"""
    resolved = [pdf if item == PDF_PLACEHOLDER else item for item in action.argv]
    return [sys.executable] + [os.path.join(directory, resolved[0])] + resolved[1:]


def clear_screen():
    if os.name == "nt":
        os.system("cls")


def print_menu(pdf):
    print("=" * 60)
    print("  FENTAY B2B 訂單解析工具")
    print("=" * 60)
    print("  工作目錄 : %s" % HERE)
    print("  直譯器   : %s" % sys.executable)
    print("  PDF 檔案 : %s" % (pdf if pdf else "(未找到)"))
    print()
    for action in ACTIONS:
        print("  %-52s" % action.label())
    print()
    print("  Select [%s]: " % "/".join(a.key for a in ACTIONS), end="", flush=True)


def confirm(message):
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def pause_menu():
    print()
    print("-" * 60)
    try:
        input("按 Enter 返回選單...")
    except EOFError:
        # 非互動環境（例如管線輸入）：不再回到選單，避免無限迴圈
        return False
    return True


def run_action(action, pdf, directory=HERE):
    """
    執行一個選項。

    回傳 True 表示應該回到選單，False 表示使用者要離開。
    前置條件不符時只擋下這個選項，其他選項不受影響。
    """
    clear_screen()

    if action.needs_pdf and not pdf:
        print("[錯誤] 此選項需要 PDF，但目錄內找不到 *.pdf")
        print("       請放入 PDF 後重試，或選擇其他選項。")
        return True

    missing = missing_requirements(action, directory)
    if missing:
        print("[錯誤] 此選項需要以下檔案，目前缺少：")
        for name in missing:
            print("       - %s" % name)
        return True

    if action.confirm and not confirm(action.confirm):
        print("已取消。")
        return True

    if action.argv:
        argv = build_argv(action, pdf, directory)
        # 顯示用：把 {pdf} 換成實際檔名，但保留相對路徑以維持可讀性
        display = " ".join([action.script] +
                           [pdf if item == PDF_PLACEHOLDER else item
                            for item in action.argv[1:]])
        print("[執行中] %s" % display)
        print("-" * 60)
        try:
            code = subprocess.call(argv, cwd=directory)
        except OSError as exc:
            print("[錯誤] 無法啟動 %s: %s" % (action.script, exc))
            return True
        print("-" * 60)
        if code != 0:
            print("[警告] %s 以代碼 %d 結束，請看上面的訊息。" % (action.script, code))
        else:
            print("[完成] %s" % action.script)
    return True


def main():
    os.chdir(HERE)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if "--list" in sys.argv:
        print("選項表：")
        for action in ACTIONS:
            print("  %-52s argv=%s" % (action.label(), action.argv))
        return 0

    actions_by_key = {action.key: action for action in ACTIONS}
    if len(actions_by_key) != len(ACTIONS):
        sys.exit("[錯誤] ACTIONS 有重複的選項編號")

    while True:
        pdf = find_pdf()
        print_menu(pdf)
        try:
            choice = input().strip()
        except EOFError:
            print()
            return 0

        action = actions_by_key.get(choice)
        if action is None:
            print("[警告] 無效的選項：%r" % choice)
            if not pause_menu():
                return 0
            continue

        if action.exit:
            return 0

        if not run_action(action, pdf):
            return 0

        if not pause_menu():
            return 0


if __name__ == "__main__":
    sys.exit(main())