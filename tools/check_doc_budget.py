# -*- coding: utf-8 -*-
"""check_doc_budget.py — 進行系ドキュメントの肥大を止める「文書予算」ガード。

何を守るか: 台帳・ダイジェストなど「毎セッション読むファイル」は放置すると
仕様詳細や完結済みの経緯が漏れ込んで際限なく太る（実測: 状態台帳が1行7,370字・
46KBまで肥大）。減量後の**逆流（再肥大）**を人の注意でなく機械でブロックする。

設定: リポジトリ直下の doc-budget.json（--config で変更可）
{
  "warn_ratio": 0.95,
  "budgets":    { "spec/STATE-LEDGER.md": 16000, "spec/SESSION-DIGEST.md": 24000 },
  "row_limits": { "spec/STATE-LEDGER.md": 600 },
  "section_rules": {
    "spec/SESSION-DIGEST.md": {"heading": "^## 20\\\\d\\\\d-", "max_sections": 4,
                               "max_section_bytes": 3000},
    "spec/research-index.md": {"heading": "^## ", "max_section_bytes": 1000,
                               "baseline": {"4.11": 1383}}
  }
}
- budgets:    ファイルごとのバイト予算（超過で exit 1）
- row_limits: mdテーブル行（`|` 始まり）の1行の字数上限（仕様漏出の防止線）
- warn_ratio: 予算に対する早期警告の閾値（既定 0.85）。「超過」でなく「接近」で鳴らす
- section_rules: 節単位ratchet。heading 正規表現に合う `## ` 節を対象に、
  節数上限(max_sections)と節バイト上限(max_section_bytes)を検査。節の終端=次の任意の `^## ` 行。
  baseline: 既存肥大§の現サイズ凍結（キー=見出しの `## ` 直後トークン）。許容= max(baseline, 上限)
  ＝増加のみ禁止（Strangler: 増やす時は詳細を該当specへ移してから薄くする）。

超過時の処方: 原文を archive/ ディレクトリへ丸ごと移送（情報ロスゼロ・grep可能）し、
元ファイルには「状態＋次の一手＋ポインタ」だけを残す。移送作業そのものの機械化＝
tools/rotate_digest.py（節を回転）・tools/rotate_ledger.py（行を回転）。

実行:  python tools/check_doc_budget.py              (0=予算内 / 1=超過)
       python tools/check_doc_budget.py --self-test  (合成フィクスチャで自己検証)
依存: Python 3.8+ 標準ライブラリのみ。
"""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_CONFIG = "doc-budget.json"
ROTATE_HINT = ("移送コマンド: 節= python tools/rotate_digest.py --apply ／ "
               "行= python tools/rotate_ledger.py --apply（既定 dry-run・逐語移送）")
WARN_RATIO_DEFAULT = 0.85


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return (cfg.get("budgets", {}),
            cfg.get("row_limits", {}),
            float(cfg.get("warn_ratio", WARN_RATIO_DEFAULT)),
            cfg.get("section_rules", {}))


def split_sections(text, heading_pat):
    """heading_pat に合う `## ` 節を (キー, 見出し行, バイト数, 行番号) で返す。

    節の終端は「次の任意の ^## 行」（対象外見出しも境界になる＝日付節の直後に
    通常節が来ても日付節へ混入させない）。キー=見出しの `## ` 直後トークン
    （例 "4.11" / "2026-07-23"）＝baseline 照合用。
    """
    pat = re.compile(heading_pat)
    lines = text.splitlines(keepends=True)
    marks = [i for i, l in enumerate(lines) if l.startswith("## ")]
    out = []
    for n, i in enumerate(marks):
        if not pat.match(lines[i]):
            continue
        j = marks[n + 1] if n + 1 < len(marks) else len(lines)
        body = "".join(lines[i:j])
        m = re.match(r"^##\s+(\S+)", lines[i])
        key = m.group(1) if m else lines[i].strip()
        out.append((key, lines[i].rstrip(), len(body.encode("utf-8")), i + 1))
    return out


def check_sections(root, section_rules):
    """節単位ratchetの違反リストと、baseline整理の助言リストを返す。"""
    problems, advices = [], []
    for rel, rule in section_rules.items():
        p = root / rel
        if not p.is_file():
            problems.append(f"{rel}: section_rules に記載があるがファイルが存在しない（パス確認）")
            continue
        secs = split_sections(p.read_text(encoding="utf-8"), rule["heading"])
        max_n = rule.get("max_sections")
        if max_n is not None and len(secs) > max_n:
            problems.append(
                f"{rel}: 対象節 {len(secs)}個 > 上限{max_n}個"
                f" → 完結した節を archive/ へ移送せよ（完結即ローテ。"
                f"python tools/rotate_digest.py --apply）")
        cap = rule.get("max_section_bytes", 0)
        baseline = rule.get("baseline", {})
        for key, heading, nbytes, lineno in secs:
            allowed = max(baseline.get(key, 0), cap)
            if nbytes > allowed:
                problems.append(
                    f"{rel}:{lineno}: 節{nbytes:,}B > 許容{allowed:,}B"
                    f" → 詳細を該当specへ移して節を薄く: {heading[:40]}…")
            elif key in baseline and nbytes <= cap:
                advices.append(
                    f"{rel}: §{key} は{nbytes:,}B≤{cap:,}B に痩せた"
                    f" → doc-budget.json の baseline \"{key}\" 行を削除可（再肥大の天井を締める）")
    return problems, advices


def near_limit(root, budgets, warn_ratio):
    """予算内だが warn_ratio 以上＝接近中のファイル（早期警告・exitは変えない）。

    なぜ必要か: 予算ガードが「超過で落ちる」だけだと、96.9%でも表示は PASS のまま＝
    接近に気づけるのは人が思い出した時だけになる（＝注意頼み）。実測では索引ファイルが
    96.9%まで無警告で進み、人の指摘で初めて発覚した（2026-07-21）。
    """
    out = []
    for rel, limit in budgets.items():
        p = root / rel
        if not p.is_file():
            continue
        size = p.stat().st_size
        if limit * warn_ratio <= size <= limit:
            out.append(f"{rel}: {size:,}B / 予算{limit:,}B = {size / limit * 100:.1f}%"
                       f"（残り{limit - size:,}B）")
    return out


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

        # 早期警告（digest.md=100B）: 閾値0.85 の上下と、超過側は警告に含めないこと
        warn_cases = [
            ("接近せず(50%)=非警告", {"digest.md": 200}, False),
            ("接近(90.9%)=警告", {"digest.md": 110}, True),
            ("閾値直下(83.3%)=非警告", {"digest.md": 120}, False),
            ("超過(200%)は警告でなくエラー側", {"digest.md": 50}, False),
        ]
        for label, budgets, should_warn in warn_cases:
            warned = bool(near_limit(root, budgets, WARN_RATIO_DEFAULT))
            good = warned == should_warn
            ok &= good
            print(f"  [{'PASS' if good else '** FAIL **'}] {label}: 警告={warned} (期待={should_warn})")

        # 節単位ratchet: 日付節（節境界=任意の^##行＝直後の通常節を混入させない）
        (root / "digest2.md").write_text(
            "# t\n"
            "## 2026-07-01 a\nx\n"
            "## 2026-07-02 b\nx\n"
            "## 2026-07-03 c\n" + "y" * 500 + "\n"
            "## ハマりどころ\n" + "z" * 9000 + "\n",   # 対象外節は数も大きさも見ない
            encoding="utf-8")
        # 索引ratchet: baseline凍結の発火/非発火
        (root / "index2.md").write_text(
            "# t\n"
            "## 4.11 big\n" + "a" * 1500 + "\n"
            "## 4.12 small\nok\n",
            encoding="utf-8")
        digest_rule = {"heading": r"^## 20\d\d-", "max_section_bytes": 3000}
        sec_cases = [
            ("日付節数=上限内・対象外節は不算入=非発火",
             {"digest2.md": dict(digest_rule, max_sections=3)}, False),
            ("日付節数超過=発火",
             {"digest2.md": dict(digest_rule, max_sections=2)}, True),
            ("日付節の肥大=発火（境界=次の任意^##）",
             {"digest2.md": dict(digest_rule, max_sections=9, max_section_bytes=400)}, True),
            ("索引§がcap超過・baseline無し=発火",
             {"index2.md": {"heading": "^## ", "max_section_bytes": 1000}}, True),
            ("索引§がbaseline以内=非発火（凍結ratchet）",
             {"index2.md": {"heading": "^## ", "max_section_bytes": 1000,
                            "baseline": {"4.11": 1600}}}, False),
            ("索引§がbaseline超過=発火（増加のみ禁止）",
             {"index2.md": {"heading": "^## ", "max_section_bytes": 1000,
                            "baseline": {"4.11": 1400}}}, True),
        ]
        for label, rules, should_fire in sec_cases:
            fired = bool(check_sections(root, rules)[0])
            good = fired == should_fire
            ok &= good
            print(f"  [{'PASS' if good else '** FAIL **'}] {label}: 発火={fired} (期待={should_fire})")
        # 痩せた§のbaseline整理助言（非発火・助言のみ）
        probs, advices = check_sections(
            root, {"index2.md": {"heading": "^## ", "max_section_bytes": 1000,
                                 "baseline": {"4.11": 1600, "4.12": 900}}})
        good = (probs == []) and ("4.12" in " ".join(advices)) and ("4.11" not in " ".join(advices))
        ok &= good
        print(f"  [{'PASS' if good else '** FAIL **'}] 痩せ§のbaseline削除助言=出る/巻添え無し")
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
    budgets, row_limits, warn_ratio, section_rules = load_config(cfg_path)
    problems = check(args.root, budgets, row_limits)
    sec_problems, sec_advices = check_sections(args.root, section_rules)
    problems += sec_problems
    if problems:
        print(f"** 文書予算超過 {len(problems)}件 **")
        for pr in problems:
            print(f"  {pr}")
        print("→ 処方: 原文を archive/ へ移送して薄い行に戻す（who-reads-when パターンF/G）")
        print(f"   {ROTATE_HINT}")
        return 1
    warns = near_limit(args.root, budgets, warn_ratio)
    if warns:
        print(f"▲ 予算接近 {len(warns)}件（{warn_ratio:.0%}超・まだ通す）")
        for w in warns:
            print(f"  {w}")
        print("→ 今のうちに archive/ へ移送を（who-reads-when パターンF/G）")
        print(f"   {ROTATE_HINT}")
    for a in sec_advices:
        print(f"ℹ {a}")
    print("PASS: 進行系ドキュメントは予算内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
