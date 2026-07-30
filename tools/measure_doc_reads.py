# -*- coding: utf-8 -*-
"""measure_doc_reads.py — どの md を・何割のセッションで・いつ・何回読んだかを実測する。

予算ガード（検出層）に対して、これは**観測層**＝実物を見る目。
「読むべき人が読むべき時に必要なものだけ読む」を、体感でなくログで検証する。

なぜ要るか（実測 2026-07-31・64セッション）:
- **配達方式が使用率を決めていた**。フック/パススコープで機械配達される文書は 16% の
  セッションで読まれ、CLAUDE.md の分岐参照表（「該当時に Read せよ」）経由は 0〜5%。
  最大 17.9KB の規範ファイルが 5%、6.9KB の品質規範は **64セッションで0回**だった。
  リンクと予算だけあって誰も開かない文書は、削っても増やしても何も変わらない。
- **症状は「予算超過」でなく「1回で入らない」**。起動時に読む台帳の再読が 3.1回/セッション
  ＝方向づけの文書を3回開き直すのは内容が一度で入っていない信号で、バイト予算は
  その代理指標にすぎなかった。案件専属specの高再読（5.8回）は健全（実装中の行き来）。

読み方:
  cover  何割のセッションで1回以上読まれたか → 必要度の tier
  first  初回読取がセッション内の何%地点か   → 0%付近=起動時に要る / 中盤=作業中に引く
  re     読んだセッションあたりの再読回数     → **役割で意味が反転**（上記）

限界（結論を出す前に必ず添える）:
  ① 別マシンのログは無い ② ツールの grep 参照は Read として記録されない
  ③ 委譲先（サブエージェント）の記録は <session-id>/ サブディレクトリ＝直下だけ走査すると
     丸ごと落ちる（初版はこれで「未読46件」という誤った結論を出した）
  → **読まれた側の数字だけが信用できる。未読は「使われていない」の証拠にならない**
    ＝未読を理由に文書を消さない（辞書は全残し・品質は引けること）。

使い方:
  python tools/measure_doc_reads.py --slug <~/.claude/projects/ 配下の名前>
  python tools/measure_doc_reads.py --slug ... --since 2026-07-31
  --since は仮説検証用: 文書を改編した日以降だけを測る。改編前のセッションを混ぜると
  再読回数がベースライン込みになり、改善したかどうか結論が出ない（交絡）。
依存: Python 3.8+ 標準ライブラリのみ。
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

PAT = re.compile(r'"name":"Read","input":\{"file_path":"(.*?)"')


def reads_in(path):
    """1つの JSONL から Read された file_path を出現順に返す。"""
    out = []
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"name":"Read"' in line:
                out += [m.group(1).replace("\\\\", "\\") for m in PAT.finditer(line)]
    return out


def normalize(p, repo):
    """repo 相対 → だめならホーム相対。basename に潰すと同名ファイル
    （SKILL.md 同士など）が1行に合流して別物を同一視する（実害あり）。"""
    q = Path(p).resolve()
    try:
        return str(q.relative_to(repo)).replace("\\", "/")
    except (ValueError, OSError):
        pass
    try:
        return "~/" + str(q.relative_to(Path.home())).replace("\\", "/")
    except (ValueError, OSError):
        return str(q).replace("\\", "/")


def collect(logs_dir, repo, since_ts=None):
    """セッション = <id>.jsonl（main）+ <id>/**.jsonl（委譲先）の合併で1つと数える。"""
    cover, firstpos, total = {}, {}, {}
    sessions = sorted(logs_dir.glob("*.jsonl"))
    if since_ts is not None:
        sessions = [s for s in sessions if s.stat().st_mtime >= since_ts]
    for s in sessions:
        order = reads_in(s)
        sub = logs_dir / s.stem
        if sub.is_dir():
            for f in sorted(sub.rglob("*.jsonl")):
                order += reads_in(f)
        order = [p for p in order if p.lower().endswith(".md")]
        if not order:
            continue
        n, seen = len(order), {}
        for i, p in enumerate(order):
            rel = normalize(p, repo)
            total[rel] = total.get(rel, 0) + 1
            cover.setdefault(rel, set()).add(s.name)
            seen.setdefault(rel, i / n)
        for rel, pos in seen.items():
            firstpos.setdefault(rel, []).append(pos)
    return len(sessions), cover, firstpos, total


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="md読取の実測（cover/first/re）")
    ap.add_argument("--slug", required=True,
                    help="~/.claude/projects/ 配下のプロジェクト名（cwd由来のslug）")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="リポジトリルート")
    ap.add_argument("--min-reads", type=int, default=3, help="この回数未満のdocは出さない")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="この日以降に動いたセッションだけ測る（改編の前後を混ぜない）")
    a = ap.parse_args()

    logs = Path.home() / ".claude/projects" / a.slug
    if not logs.is_dir():
        print(f"ログが無い: {logs}", file=sys.stderr)
        return 1
    since_ts = datetime.strptime(a.since, "%Y-%m-%d").timestamp() if a.since else None

    n, cover, firstpos, total = collect(logs, a.root.resolve(), since_ts)
    if n == 0:
        print(f"対象セッション 0（--since {a.since} 以降のログが無い）")
        return 0
    rows = sorted(((len(v) / n, median(firstpos[k]), total[k], k) for k, v in cover.items()),
                  reverse=True)
    scope = f"・--since {a.since} 以降" if a.since else ""
    print(f"対象 {n} セッション（main + 委譲先を合併{scope}）\n")
    print("  cover  first  reads     re  doc")
    for c, f, t, d in rows:
        if t < a.min_reads:
            continue
        print(f"  {c:>5.0%}  {f:>5.0%}  {t:>5}  {t/(c*n):>5.1f}  {d}")
    print("\n※未読は判定不能（docstring の限界①〜③）。未読を理由に文書を消さないこと。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
