# -*- coding: utf-8 -*-
"""rotate_ledger.py — 状態台帳の完結した「行」を archive へ回転させる。

rotate_digest.py と同じ問題（検出は足りていた／欠けていたのは移送の手作業）だが、
**規則は同じではない**ことが要点。digest は「最新以外の日付節＝完結」が規約由来の
機械規則なのに対し、台帳の行にそれは存在しない。LIVE の ✅ 行は「残=」を抱えており
（例 `voice_pack/plan` ✅ 残=README）、状態語だけで自動移送すると
**次の一手を消す＝現在地の破壊**になる。よって機械化を2段に分ける:

- **全自動**: `## 直近クローズ` 節は台帳自身が「文脈用・**数行で維持**」と規定＝
  keep-N が規約由来の機械規則。表は新しい順に並ぶ運用なので**上から N 行**残す。
- **半自動**: それ以外（LIVE / 検証予約）は `--rows` で**行番号を明示**して逐語移送する。
  判断（この行はもう完結か）は人が持ち、機械は移送だけを持つ。LIVE 行は
  `--allow-live` が無い限り拒否＝現在地の破壊を既定で不可能にする。

不変条件（「変な編集をしない」保証）:
 (a) 消す前に archive を**読み直して**逐語保存を確認し、欠けていたら台帳を
     **一切書き換えずに中止**する（同名キー別内容での本文欠落を捕捉）。
 (b) 残る行は1文字も触らない（末尾空白の正規化すらしない）＝行の削除だけを行う。

やらないこと（意図的な非目標）: 状態語による自動判定 / 全史ファイルへの要約書き込み
（要約は判断＝機械が書くと嘘が入る）/ 自動 commit / 自動 push / archive 既存本文の書換。

実行:
  python tools/rotate_ledger.py                     # 既定=dry-run（何が動くかだけ表示）
  python tools/rotate_ledger.py --apply
  python tools/rotate_ledger.py --rows 51 --apply   # 個別行も一緒に（行番号は dry-run 表示のもの）
  python tools/rotate_ledger.py --self-test
依存: Python 3.8+ 標準ライブラリのみ。
"""
import argparse
import datetime
import re
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rotate_digest import (INDEX_NAME, Layout, insert_index,  # noqa: E402
                           machine_tag)  # INDEX追記規則・機械タグの実装は1つ

DEFAULT_TARGET = "spec/STATE-LEDGER.md"
DEFAULT_ARCHIVE = "spec/archive"
INDEX_SECTION = "## 台帳行退避"
LIVE_PREFIX = "## LIVE"
CLOSED_PREFIX = "## 直近クローズ"
DEFAULT_CLOSE_KEEP = 2
KEY_MAX = 60
SEPARATOR = re.compile(r"^\|[\s:\-|]+\|\s*$")

# 「どの行を選ぶか」の方針。節見出しもここに置く（=選別の語彙であって置き場ではない）。
Policy = namedtuple("Policy", "close_keep rows allow_live live_prefix closed_prefix")


def layout_of(target=DEFAULT_TARGET, archive_dir=DEFAULT_ARCHIVE, index_section=INDEX_SECTION):
    return Layout(target, archive_dir, index_section)


def policy_of(close_keep=DEFAULT_CLOSE_KEEP, rows=(), allow_live=False,
              live_prefix=LIVE_PREFIX, closed_prefix=CLOSED_PREFIX):
    return Policy(close_keep, list(rows), allow_live, live_prefix, closed_prefix)


# ---- 解析（純粋関数。I/O は下の rotate() 側にまとめる） ----

def sections(lines):
    """`## ` 見出しごとに (見出し行, start, end) を返す（end 排他・見出し行を含む）。"""
    marks = [i for i, l in enumerate(lines) if l.startswith("## ")]
    return [(lines[i].rstrip(), i, marks[n + 1] if n + 1 < len(marks) else len(lines))
            for n, i in enumerate(marks)]


def data_rows(lines, start, end):
    """表の**データ行**の行index（見出し行・区切り行は含まない）。

    区切り行（`|---|---|`）以降の `|` 行をデータとみなす＝列数や見出し文言に依存しない。
    """
    pipes = [i for i in range(start, end) if lines[i].startswith("|")]
    seps = [i for i in pipes if SEPARATOR.match(lines[i])]
    return [i for i in pipes if seps and i > seps[0]]


def row_key(line):
    """行の第1セルをキー化（archive の `## <key>` 見出し＝重複移送の判定にも使う）。"""
    cell = line.split("|")[1] if line.count("|") >= 2 else line
    return (cell.strip().strip("`").strip() or "row")[:KEY_MAX]


def select(lines, policy):
    """移送する (行index, 節名) を昇順で返す。拒否があれば ([], 理由リスト)。"""
    secs = sections(lines)
    picks, refusals = {}, []

    closed = next((s for s in secs if s[0].startswith(policy.closed_prefix)), None)
    if closed:
        for i in data_rows(lines, closed[1], closed[2])[policy.close_keep:]:
            picks[i] = closed[0]

    for n in policy.rows:
        i = n - 1
        sec = next((s for s in secs if s[1] <= i < s[2]), None)
        if not (0 <= i < len(lines)) or sec is None:
            refusals.append(f"L{n}: 節の外（見出しより前）か行番号が範囲外")
        elif i not in data_rows(lines, sec[1], sec[2]):
            refusals.append(f"L{n}: 表のデータ行ではない（見出し/区切り/本文）: {lines[i].strip()[:40]}")
        elif sec[0].startswith(policy.live_prefix) and not policy.allow_live:
            refusals.append(f"L{n}: LIVE行は既定で移送しない（現在地の破壊防止。意図的なら --allow-live）")
        else:
            picks[i] = sec[0]

    return ([] if refusals else sorted(picks.items())), refusals


# ---- 出力 ----

def archive_name(layout, stamp, tag):
    return f"{Path(layout.target).stem.lower()}-rows-{stamp}-{tag}.md"


def archive_body(picks, lines):
    return "".join(f"## {row_key(lines[i])}\n\n```\n{lines[i].rstrip()}\n```\n\n" for i, _sec in picks)


def write_archive(path, layout, picks, lines, stamp):
    """archive へ追記（新規なら見出しを付ける）。既に載っているキーは本文を書かない。"""
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not existing:
        existing = (f"# {Path(layout.target).stem} 行アーカイブ（{stamp} 減量時の原文・追記専用）\n\n"
                    f"> 移送元=`{layout.target}`（逐語）。**状態の現在値は台帳本体が正**。\n"
                    f"> ここは「当時の行に何が書いてあったか」を grep で引くための保全層（手動編集しない）。\n\n")
    fresh = [p for p in picks if f"## {row_key(lines[p[0]])}" not in existing]
    path.write_text(existing + archive_body(fresh, lines), encoding="utf-8")
    return fresh


def index_lines(picks, lines, fname, stamp):
    return [f"- {stamp} `{row_key(lines[i])}`（{sec.split('（')[0][3:].strip()}"
            f"・退避=[{fname}]({fname})）\n" for i, sec in picks]


def rotate(root, layout, policy, stamp, tag, apply_):
    path = root / layout.target
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    picks, refusals = select(lines, policy)
    if refusals:
        print("移送しません（1件でも拒否があれば部分適用しない）:")
        for r in refusals:
            print(f"  - {r}")
        return 1
    if not picks:
        print(f"回転不要: 直近クローズは {policy.close_keep} 行以下・--rows の指定なし")
        return 0

    fname = archive_name(layout, stamp, tag)
    freed = sum(len(lines[i].encode("utf-8")) for i, _s in picks)
    print(f"{'移送' if apply_ else 'DRY-RUN: 移送予定'} {len(picks)}行 → {layout.archive_dir}/{fname}"
          f"（{Path(layout.target).name} から {freed:,}B 回収）")
    for i, sec in picks:
        print(f"  - L{i + 1:<3} [{sec.split('（')[0][3:].strip()}] {row_key(lines[i])}")
    if not apply_:
        print("→ 実行するなら --apply（自動 commit / push はしない）")
        return 0

    arc = root / layout.archive_dir / fname
    arc.parent.mkdir(parents=True, exist_ok=True)
    fresh = write_archive(arc, layout, picks, lines, stamp)
    if len(fresh) != len(picks):
        print(f"  （{len(picks) - len(fresh)}行は同名キーが archive に既存のため本文は再移送せず）")
    insert_index(root / layout.archive_dir / INDEX_NAME,
                 index_lines(picks, lines, fname, stamp), layout.index_section)

    # 不変条件(a): 消す前に「消す行が archive に逐語で在る」ことを**書き込んだファイルから
    # 読み直して**確かめる。循環しない検査＝同じキーの行が2本ある等で write_archive が
    # 本文を落とした場合を捕まえる（検知できたら台帳は一切書き換えずに中止＝
    # データが消えるより止まる方を選ぶ）。
    saved = arc.read_text(encoding="utf-8")
    missing = [i for i, _s in picks if lines[i].rstrip() not in saved]
    if missing:
        print(f"** 中止: {len(missing)}行が archive に逐語保存されていない（台帳は書き換えていません）**")
        for i in missing:
            print(f"  - L{i + 1} {row_key(lines[i])}")
        return 1

    # 不変条件(b): 残す行は1文字も触らない＝行の削除だけを行う。
    out = [l for n, l in enumerate(lines) if n not in {i for i, _s in picks}]
    path.write_text("".join(out), encoding="utf-8")
    print(f"完了: {layout.target} {len(''.join(out).encode('utf-8')):,}B"
          f" / {INDEX_NAME} へ {len(picks)}行追記")
    print("→ 次: python tools/check_doc_budget.py で予算を確認（要約の反映と commit は手動）")
    return 0


# ---- 自己検証 ----

FIXTURE = ("# L\n\n> 運用\n\n最終更新: x\n\n"
           "## LIVE（進行中 / 次の一手）\n\n| ID | 状態 |\n|---|---|\n"
           "| `a/1` | 🟡進行中 |\n| `a/2` | ✅完了 残=README |\n\n"
           "## 直近クローズ（文脈用・数行で維持）\n\n| ID | 状態 |\n|---|---|\n"
           "| `c/new` | ✅ |\n| `c/mid` | ✅ |\n| `c/old` | ✅ |\n\n"
           "## 検証予約\n\n| 項目 | どこで確定 |\n|---|---|\n"
           "| 予約A | いつか |\n| 予約B 完結 | ✅済 |\n")
LIVE_ROW = 11      # `| \`a/1\` |`（1-based）
SEP_ROW = 10       # `|---|---|`（LIVE表の区切り）
RESERVE_ROW = 27   # `| 予約B 完結 |`
SELFTEST_KEEP = 1  # 自己テストは既定値でなく「機構」を検証する
SELFTEST_TAG = "testpc"


def self_test():
    ok = True

    def eq(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  [{'PASS' if good else '** FAIL **'}] {label}: {got!r}" + ("" if good else f" != {want!r}"))

    lay = layout_of()
    idx_rel = f"{lay.archive_dir}/{INDEX_NAME}"

    def run(root, apply_=False, **kw):
        return rotate(root, lay, policy_of(close_keep=SELFTEST_KEEP, **kw),
                      "20260726", SELFTEST_TAG, apply_)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / lay.archive_dir).mkdir(parents=True)
        (root / lay.target).write_text(FIXTURE, encoding="utf-8")
        (root / idx_rel).write_text("# idx\n\n## 台帳行退避（時系列索引）\n- 既存行\n\n## 他の節\n- x\n",
                                    encoding="utf-8")

        # フィクスチャの行番号定数が実物とズレていないこと（テスト自身の嘘を防ぐ）
        fx = FIXTURE.splitlines(keepends=True)
        eq("LIVE_ROW/RESERVE_ROW が正しい行を指す",
           (row_key(fx[LIVE_ROW - 1]), row_key(fx[RESERVE_ROW - 1])), ("a/1", "予約B 完結"))

        eq("LIVE行は既定で拒否＝1で落ちる", run(root, True, rows=[LIVE_ROW]), 1)
        eq("拒否時は書き換えない", (root / lay.target).read_text(encoding="utf-8") == FIXTURE, True)
        eq("区切り行の指定を拒否", run(root, True, rows=[SEP_ROW]), 1)
        eq("節外(前文)の指定を拒否", run(root, True, rows=[3]), 1)
        eq("dry-run は書き換えない",
           (run(root), (root / lay.target).read_text(encoding="utf-8") == FIXTURE), (0, True))

        eq("apply", run(root, True, rows=[RESERVE_ROW]), 0)
        led = (root / lay.target).read_text(encoding="utf-8")
        arc_rel = f"{lay.archive_dir}/state-ledger-rows-20260726-{SELFTEST_TAG}.md"
        eq("ファイル名に機械タグが入る（無印は作らない）",
           ((root / arc_rel).is_file(),
            (root / f"{lay.archive_dir}/state-ledger-rows-20260726.md").is_file()), (True, False))
        arc = (root / arc_rel).read_text(encoding="utf-8")
        idx = (root / idx_rel).read_text(encoding="utf-8")
        eq("直近クローズの古い2行が消えた", ("c/mid" in led, "c/old" in led), (False, False))
        eq("keep分と他節は残る", ("c/new" in led, "a/1" in led and "a/2" in led, "予約A" in led),
           (True, True, True))
        eq("指定した検証予約行だけ消えた", "予約B" in led, False)
        eq("表の骨格(見出し・区切り)を壊さない", led.count("|---|---|"), 3)
        eq("archive に逐語で入る", ("| `c/mid` | ✅ |" in arc, "| 予約B 完結 | ✅済 |" in arc), (True, True))
        eq("INDEX へ3行追記・既存行は保持", (idx.count("退避=[state-ledger-rows"), "既存行" in idx), (3, True))
        eq("INDEX の後続節を壊さない", idx.rstrip().endswith("- x"), True)

        eq("2回目は no-op",
           (run(root, True), (root / lay.target).read_text(encoding="utf-8") == led), (0, True))

        # 同一キーの再移送防止（keep=0 で残り1行を同じファイルへ回す＝本文重複しない）
        rotate(root, lay, policy_of(close_keep=0), "20260726", SELFTEST_TAG, True)
        arc2 = (root / arc_rel).read_text(encoding="utf-8")
        eq("同名キーは本文を二重に書かない", arc2.count("| `c/mid` | ✅ |"), 1)
        eq("追加分の本文は入る", "| `c/new` | ✅ |" in arc2, True)

        # 不変条件(a): 同名キーだが中身が違う行＝archive に本文が入らない → 消さずに中止
        (root / lay.target).write_text(
            "# L\n\n## 直近クローズ（文脈用・数行で維持）\n\n| ID | 状態 |\n|---|---|\n"
            "| `c/mid` | ✅ 別の中身 |\n", encoding="utf-8")
        before = (root / lay.target).read_text(encoding="utf-8")
        eq("同名キー別内容は中止する",
           rotate(root, lay, policy_of(close_keep=0), "20260726", SELFTEST_TAG, True), 1)
        eq("中止時に行を消さない", (root / lay.target).read_text(encoding="utf-8") == before, True)

        # 対象節が無いファイルで落ちない
        (root / lay.target).write_text("# L\n\n## LIVE\n\n| ID |\n|---|\n| `z` |\n", encoding="utf-8")
        eq("対象節ゼロ=正常終了", run(root, True), 0)

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="状態台帳の完結した行を archive へ回転させる")
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--target", default=DEFAULT_TARGET, help=f"回転させる台帳（既定 {DEFAULT_TARGET}）")
    ap.add_argument("--archive-dir", default=DEFAULT_ARCHIVE, help=f"移送先ディレクトリ（既定 {DEFAULT_ARCHIVE}）")
    ap.add_argument("--close-keep", type=int, default=DEFAULT_CLOSE_KEEP,
                    help=f"「直近クローズ」に残す新しい行の数（既定 {DEFAULT_CLOSE_KEEP}）")
    ap.add_argument("--rows", default="", help="追加で移送する行番号（カンマ区切り。dry-run の表示に従う）")
    ap.add_argument("--allow-live", action="store_true", help="LIVE行の移送を許可（既定は拒否）")
    ap.add_argument("--live-prefix", default=LIVE_PREFIX, help=f"LIVE節の見出し接頭辞（既定 {LIVE_PREFIX}）")
    ap.add_argument("--closed-prefix", default=CLOSED_PREFIX,
                    help=f"全自動で回す節の見出し接頭辞（既定 {CLOSED_PREFIX}）")
    ap.add_argument("--tag", default=None, help="ファイル名の機械タグ（既定=ホスト名を正規化）")
    ap.add_argument("--apply", action="store_true", help="実際に移送する（既定は dry-run）")
    ap.add_argument("--date", default=None, help="移送先ファイル名の日付（既定=今日 YYYYMMDD）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    rows = [int(x) for x in args.rows.replace(" ", "").split(",") if x]
    stamp = args.date or datetime.date.today().strftime("%Y%m%d")
    return rotate(args.root, layout_of(args.target, args.archive_dir),
                  policy_of(args.close_keep, rows, args.allow_live,
                            args.live_prefix, args.closed_prefix),
                  stamp, args.tag or machine_tag(), args.apply)


if __name__ == "__main__":
    sys.exit(main())
