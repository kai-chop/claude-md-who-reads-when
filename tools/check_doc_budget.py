# -*- coding: utf-8 -*-
"""check_doc_budget.py — 進行系ドキュメントの肥大を止める「文書予算」ガード。

何を守るか: 台帳・ダイジェストなど「毎セッション読むファイル」は放置すると
仕様詳細や完結済みの経緯が漏れ込んで際限なく太る（実測: 状態台帳が1行7,370字・
46KBまで肥大）。減量後の**逆流（再肥大）**を人の注意でなく機械でブロックする。

設定: リポジトリ直下の doc-budget.json（--config で変更可）
{
  "budgets":    { "spec/STATE-LEDGER.md": 16000, "spec/SESSION-DIGEST.md": 24000 },
  "row_limits": { "spec/STATE-LEDGER.md": 600 }
}
- budgets:    ファイルごとのバイト予算（超過で exit 1）
- row_limits: mdテーブル行（`|` 始まり）の1行の字数上限（仕様漏出の防止線）

超過時の処方: 原文を archive/ ディレクトリへ丸ごと移送（情報ロスゼロ・grep可能）し、
元ファイルには「状態＋次の一手＋ポインタ」だけを残す。

実行:  python tools/check_doc_budget.py              (0=予算内 / 1=超過)
       python tools/check_doc_budget.py --self-test  (合成フィクスチャで自己検証)
依存: Python 3.8+ 標準ライブラリのみ。
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_CONFIG = "doc-budget.json"


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return cfg.get("budgets", {}), cfg.get("row_limits", {})


def check(root, budgets, row_limits):
    """予算違反のリストを返す（空=合格）。設定に書かれたファイルの不在は警告扱いで返す。"""
    problems = []
    for rel, limit in budgets.items():
        p = root / rel
        if not p.is_file():
            problems.append(f"{rel}: 設定に記載があるがファイルが存在しない（パス確認）")
            continue
        size = p.stat().st_size
        if size > limit:
            problems.append(f"{rel}: {size:,}B > 予算{limit:,}B → 完結分を archive/ へ移送せよ")
    for rel, limit in row_limits.items():
        p = root / rel
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("|") and len(line) > limit:
                problems.append(
                    f"{rel}:{i}: 行{len(line)}字 > {limit}字（詳細の漏出疑い）"
                    f" → 原文をarchiveへ移送し「状態+次の一手+ポインタ」へ: {line[:40]}…")
    return problems


def self_test():
    """発火/非発火の両方向を合成フィクスチャで検証する。"""
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ledger.md").write_text("# t\n| id | 短い行 |\n", encoding="utf-8")
        (root / "digest.md").write_text("x" * 100, encoding="utf-8")
        cases = [
            ("予算内=非発火", {"digest.md": 200}, {"ledger.md": 100}, False),
            ("ファイル予算超過=発火", {"digest.md": 50}, {}, True),
            ("肥大行=発火", {}, {"ledger.md": 5}, True),
            ("設定記載ファイル不在=発火", {"missing.md": 100}, {}, True),
        ]
        for label, budgets, rows, should_fire in cases:
            fired = bool(check(root, budgets, rows))
            good = fired == should_fire
            ok &= good
            print(f"  [{'PASS' if good else '** FAIL **'}] {label}: 発火={fired} (期待={should_fire})")
    print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="進行系ドキュメントの文書予算検査")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="リポジトリルート")
    ap.add_argument("--config", default=None, help=f"設定JSON（既定: <root>/{DEFAULT_CONFIG}）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    cfg_path = Path(args.config) if args.config else args.root / DEFAULT_CONFIG
    if not cfg_path.is_file():
        print(f"設定なし: {cfg_path} を作成してください（README参照）")
        return 1
    budgets, row_limits = load_config(cfg_path)
    problems = check(args.root, budgets, row_limits)
    if problems:
        print(f"** 文書予算超過 {len(problems)}件 **")
        for pr in problems:
            print(f"  {pr}")
        print("→ 処方: 原文を archive/ へ移送して薄い行に戻す（who-reads-when パターンF/G）")
        return 1
    print("PASS: 進行系ドキュメントは予算内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
