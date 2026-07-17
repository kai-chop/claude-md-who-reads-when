# -*- coding: utf-8 -*-
"""check_md_routing.py — CLAUDE.md ⇄ description の再重複（ルート逆流）を検知する。

背景: Claude Code は .claude/agents/*.md と .claude/skills/*/SKILL.md の frontmatter
`description:` をセッション開始時に自動列挙する。CLAUDE.md 本文へ同じ説明を再掲すると
毎セッション二重に注入され、二重管理でズレる（実測: 対象2節の81%が重複だった例あり）。
スリム化後の**逆流（再重複の再蓄積）**をコミット前に検知する。

判定: 各 description の正規化14字窓（日本語6字以上を含む窓）が CLAUDE.md 本文に
逐語出現するかを測り、離れた一致領域が2つ以上 or 単一領域が24字以上なら再掲=exit 1。
短い共有語彙の引用・名前やパスだけのポインタは通す。
※逐語コピペの逆流を捕る検出器。言い換えによる重複は対象外（意味判定は費用対効果外）。
※英語主体の description には日本語窓条件が合わないため、その場合は MIN_JP=0 に調整のこと。

実行:  python tools/check_md_routing.py              (0=クリーン / 1=重複あり)
       python tools/check_md_routing.py --self-test  (合成フィクスチャで自己検証)
依存: Python 3.8+ 標準ライブラリのみ。
"""
import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

WINDOW = 14      # 逐語窓の長さ(正規化後)。短いと固有名詞で誤検出、長いと言い換えを見逃す
MIN_JP = 6       # 窓に要求する日本語文字数。名前/パス(ASCII)だけのポインタ行を検査対象から外す
JP_RE = re.compile(r"[ぁ-んァ-ヶ一-龠]")


def normalize(text):
    """空白と装飾を落として逐語比較の土台を作る（改行跨ぎ・強調記号差で見逃さない）。"""
    return re.sub(r"[\s*`>|]+", "", text).lower()


def descriptions(claude_dir):
    """agents/*.md と skills/*/SKILL.md の description を (出所名, 本文) で列挙。"""
    out = []
    for p in sorted(claude_dir.glob("agents/*.md")):
        m = re.search(r"^description:\s*(.+)$", p.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            out.append((f"agent:{p.stem}", m.group(1)))
    for p in sorted(claude_dir.glob("skills/*/SKILL.md")):
        m = re.search(r"^description:\s*(.+)$", p.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            out.append((f"skill:{p.parent.name}", m.group(1)))
    return out


def find_duplication(root):
    """root 配下の CLAUDE.md に description の逐語窓が再掲されていないか。"""
    claude_md = root / "CLAUDE.md"
    if not claude_md.is_file():
        return []
    body = normalize(claude_md.read_text(encoding="utf-8"))
    hits = []
    for name, desc in descriptions(root / ".claude"):
        d = normalize(desc)
        matched = [i for i in range(0, max(len(d) - WINDOW, 0) + 1)
                   if len(JP_RE.findall(d[i:i + WINDOW])) >= MIN_JP and d[i:i + WINDOW] in body]
        if not matched:
            continue
        # 隣接/重複する一致窓を「領域」へ統合し、領域数と最長領域で判定する
        regions = []
        start = prev = matched[0]
        for i in matched[1:]:
            if i <= prev + WINDOW:      # 前の窓と重なる/隣接=同一領域
                prev = i
            else:
                regions.append((start, prev + WINDOW))
                start = prev = i
        regions.append((start, prev + WINDOW))
        longest = max(e - s for s, e in regions)
        if len(regions) >= 2 or longest >= 24:
            sample = d[regions[0][0]:min(regions[0][1], regions[0][0] + 30)]
            hits.append((name, f"{sample}…(領域{len(regions)}・最長{longest}字)"))
    return hits


def self_test():
    """合成フィクスチャで発火/非発火の両方向を検証する。"""
    ok = True
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ag = root / ".claude" / "agents"
        ag.mkdir(parents=True)
        (ag / "tester.md").write_text(
            "---\ndescription: 曖昧な依頼のズレを着手前に潰す専門の検証エージェントである\n---\n本文",
            encoding="utf-8")
        cases = [
            ("長い逐語再掲=発火",
             "# 規約\n- **tester**: 曖昧な依頼のズレを着手前に潰す専門の検証エージェント。詳細略。\n", True),
            ("短い共有語彙の引用=非発火",
             "# 規約\n- リリースは「着手前に潰す専門」の状態になってから行う。\n", False),
            ("ポインタのみ=非発火",
             "# 規約\n- 検証は `agents/tester.md` へ委譲する。\n", False),
            ("無関係本文=非発火",
             "# 規約\n- ビルドは dotnet build を使う。\n", False),
        ]
        for label, body, should_fire in cases:
            (root / "CLAUDE.md").write_text(body, encoding="utf-8")
            fired = bool(find_duplication(root))
            good = fired == should_fire
            ok &= good
            print(f"  [{'PASS' if good else '** FAIL **'}] {label}: 発火={fired} (期待={should_fire})")
    print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="CLAUDE.md と description の再重複検査")
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    hits = find_duplication(args.root)
    if hits:
        print(f"** 重複 {len(hits)}件 ** description の内容が CLAUDE.md 本文に再掲されている:")
        for name, win in hits:
            print(f"  {name}: …{win}…")
        print("→ 本文からは削る（descriptionが自動列挙される）。境界・例外だけ本文に残す（パターンD）。")
        return 1
    print("PASS: CLAUDE.md に description の再掲なし（ルート管理は保たれている）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
