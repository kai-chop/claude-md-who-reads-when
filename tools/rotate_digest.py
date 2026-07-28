# -*- coding: utf-8 -*-
"""rotate_digest.py — 完結した日付節を archive へ「回転」させる（移送の手作業を排除）。

何を解決するか: 進行系mdの肥大は check_doc_budget.py が既に**検出**している
（日付節が上限超で exit 1・予算95%で警告）。にもかかわらず実測では digest が
**99.2%に常駐し続けた**——検出されても**移送が手作業**だったため。
対策ヒエラルキー（排除＞観測＞検出＞注意）で言えば、検出の次に足すべきものは
2本目の検知器ではなく、**手作業そのものの排除**である。

回転の規則（判断を要しない機械規則だけを機械が持つ）:
- 日付節（doc-budget.json の `section_rules[target].heading`）は**末尾から
  --keep 個だけ残す**（既定1＝最新のみ）。「完結した節」の機械的定義＝**最新以外**
  （次の日付節が始まった時点で前の節は完結）。現在地(LIVE)は台帳側が持ち、
  digest 側は経緯でしかない、という前提に立つ（パターンF/G）。
  **前提: 節は古い→新しいの順にファイル内へ並んでいること**（日付でソートせず
  出現順を契約にする＝heading は任意の正規表現で日付とは限らないため）。逆順に
  並べる運用では最新側が移送対象になるので、dry-run の「残す節」表示で確認すること。
- 移送先 `<archive>/<target名>-rotated-<YYYYMMDD>-<tag>.md`（1回のローテ=1ファイル）。
  原文は**逐語**で移す＝情報ロスゼロ・grep可能。tag は machine_tag() を参照。
- `<archive>/INDEX.md` の「digest退避」節へ1行ずつ追記。

やらないこと（意図的な非目標）: 自動 commit / 自動 push / 台帳への書込 /
archive 既存ファイルの本文書換（追記のみ・同一キーは再移送しない）。

節の終端規則（次の任意の `^## ` 行）は check_doc_budget.split_sections と同一であり、
--self-test で同モジュールと**キー突合**して drift を機械検出する（規則の正典は1つ）。
見出し正規表現も doc-budget.json から読む＝2箇所に書かない。

実行:
  python tools/rotate_digest.py              # 既定=dry-run（何が動くかだけ表示）
  python tools/rotate_digest.py --apply      # 実移送
  python tools/rotate_digest.py --self-test  # 合成フィクスチャで自己検証
依存: Python 3.8+ 標準ライブラリのみ。
"""
import argparse
import datetime
import platform
import re
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_doc_budget import load_config, split_sections  # noqa: E402  （終端規則の正典）

DEFAULT_TARGET = "spec/SESSION-DIGEST.md"
DEFAULT_ARCHIVE = "spec/archive"
INDEX_NAME = "INDEX.md"
INDEX_SECTION = "## digest退避"
DEFAULT_KEEP = 1
TAG_MAX = 24

# 置き場の境界契約。パスを個別引数でばら撒くとフラグが増殖する（浅いモジュールの兆候）ので
# 「どこへ移送するか」を1つの値にまとめる。rotate_ledger.py も同じ型を使う。
Layout = namedtuple("Layout", "target archive_dir index_section")


def layout_of(target=DEFAULT_TARGET, archive_dir=DEFAULT_ARCHIVE, index_section=INDEX_SECTION):
    return Layout(target, archive_dir, index_section)


def machine_tag(node=None):
    """ローテ物のファイル名に付ける機械タグ（既定=ホスト名を正規化したもの）。

    なぜ必要か: 2台が同日にローテすると無印名は同一パスへ別内容を作り、pull で
    add/add 衝突になる（実発生 2026-07-26）。接尾辞を付ければ両者は別ファイルとして
    共存し、衝突自体が起こらない＝検出でなく排除。**規約文でなく命名を機械化する**のは、
    「無印を作る自動化」の方が規約文より強いため（人の注意に載せない）。
    """
    raw = (node if node is not None else platform.node()) or ""
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", raw.lower())).strip("-")
    return slug[:TAG_MAX] or "local"


def date_sections(text, heading_pat):
    """heading_pat に合う `## ` 節を (key, heading, start, end) で返す（行index・end排他）。

    終端=次の任意の `^## ` 行（対象外の見出しも境界になる＝直後の通常節を巻き込まない）。
    """
    pat = re.compile(heading_pat)
    lines = text.splitlines(keepends=True)
    marks = [i for i, l in enumerate(lines) if l.startswith("## ")]
    out = []
    for n, i in enumerate(marks):
        if not pat.match(lines[i]):
            continue
        end = marks[n + 1] if n + 1 < len(marks) else len(lines)
        m = re.match(r"^##\s+(\S+)", lines[i])
        key = m.group(1) if m else lines[i].strip()
        out.append((key, lines[i].rstrip(), i, end))
    return lines, out


def plan(root, layout, heading_pat, keep):
    """移送対象（古い順）と残す節を返す。対象が無ければ moves=[]。"""
    text = (root / layout.target).read_text(encoding="utf-8")
    lines, secs = date_sections(text, heading_pat)
    moves = secs[:-keep] if keep > 0 else list(secs)
    return lines, secs, moves


def archive_name(layout, stamp, tag):
    return f"{Path(layout.target).stem.lower()}-rotated-{stamp}-{tag}.md"


def archive_body(moves, lines):
    return "".join("".join(lines[s:e]).rstrip() + "\n\n" for _k, _h, s, e in moves)


def write_archive(path, layout, moves, lines, stamp):
    """archive ファイルへ追記（新規なら見出しを付ける）。既に載っているキーは書かない。"""
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if not existing:
        existing = (f"# {Path(layout.target).stem} 退避（{stamp} ローテ）\n\n"
                    f"> 移送元=`{layout.target}` の完結した節（逐語）。索引=`{INDEX_NAME}`。\n"
                    f"> 現在地(LIVE)は台帳側が持つ＝ここは経緯の凍結庫（手動編集しない）。\n\n")
    fresh = [m for m in moves if f"## {m[0]}" not in existing]
    path.write_text(existing + archive_body(fresh, lines), encoding="utf-8")
    return fresh


def index_lines(moves, fname):
    return [f"- {h[3:].strip()}（退避=[{fname}]({fname})）\n" for _k, h, _s, _e in moves]


def insert_index(path, new_lines, section=INDEX_SECTION):
    """INDEX.md の指定節の末尾へ追記（節が無ければ末尾に節ごと作る）。

    section を引数に持つのは rotate_ledger.py が同じ挿入規則を共有するため
    （INDEX への追記規則の実装を2箇所に書かない）。
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.is_file() else []
    head = next((i for i, l in enumerate(lines) if l.startswith(section)), None)
    if head is None:
        lines += ["\n", f"{section}（時系列索引）\n"]
        head = len(lines) - 1
    end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > head + 1 and not lines[end - 1].strip():
        end -= 1
    path.write_text("".join(lines[:end] + new_lines + lines[end:]), encoding="utf-8")


def rotate(root, layout, keep, stamp, tag, apply_):
    rule = load_config(root / "doc-budget.json")[3].get(layout.target)
    if not rule:
        print(f"doc-budget.json に {layout.target} の section_rules がありません（見出し規則の正典）")
        return 1
    lines, secs, moves = plan(root, layout, rule["heading"], keep)
    if not moves:
        print(f"回転不要: 対象節 {len(secs)}個 ≤ 残す数 {keep}")
        return 0

    fname = archive_name(layout, stamp, tag)
    freed = sum(len("".join(lines[s:e]).encode("utf-8")) for _k, _h, s, e in moves)
    print(f"{'移送' if apply_ else 'DRY-RUN: 移送予定'} {len(moves)}節 → {layout.archive_dir}/{fname}"
          f"（{Path(layout.target).name} から {freed:,}B 回収）")
    for k, h, _s, _e in moves:
        print(f"  - {k}  {h[3:][:60]}")
    # 残す節のキーを出す＝「節が古い→新しい順に並ぶ」前提が破れているファイル
    # （逆順運用）で最新側を移送しようとしていることを、--apply の前に目視で捕まえる。
    kept = [k for k, _h, _s, _e in secs[len(moves):]]
    print(f"  残す{len(kept)}節: {', '.join(kept) if kept else '（なし）'}")
    if not apply_:
        print("→ 実行するなら --apply（自動 commit / push はしない）")
        return 0

    arc = root / layout.archive_dir / fname
    arc.parent.mkdir(parents=True, exist_ok=True)
    fresh = write_archive(arc, layout, moves, lines, stamp)
    if len(fresh) != len(moves):
        print(f"  （{len(moves) - len(fresh)}節は同名キーが archive に既存のため本文は再移送せず）")
    insert_index(root / layout.archive_dir / INDEX_NAME, index_lines(moves, fname),
                 layout.index_section)

    out = list(lines)
    for _k, _h, s, e in sorted(moves, key=lambda m: m[2], reverse=True):
        del out[s:e]
    (root / layout.target).write_text("".join(out).rstrip() + "\n", encoding="utf-8")
    print(f"完了: {layout.target} {len(''.join(out).encode('utf-8')):,}B"
          f" / {INDEX_NAME} へ {len(moves)}行追記")
    print("→ 次: python tools/check_doc_budget.py で予算を確認（commit は手動）")
    return 0


FIXTURE = ("# d\n\n## 通常節\nkeep me\n\n"
           "## 2026-01-01 a\nAAA\n\n## 2026-01-02 b\nBBB\n\n"
           "## 2026-01-03 c\nCCC\n\n## 2026-01-04 d\nDDD\n\n## 後続の通常節\ntail\n")
FIX_CFG = ('{"budgets":{},"section_rules":{"spec/SESSION-DIGEST.md":'
           '{"heading":"^## 20\\\\d\\\\d-","max_sections":4,"max_section_bytes":3000}}}')
SELFTEST_KEEP = 2   # 自己テストは既定値でなく「機構」を検証する（既定を変えてもテストは動く）
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
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / lay.archive_dir).mkdir(parents=True)
        (root / lay.target).write_text(FIXTURE, encoding="utf-8")
        (root / idx_rel).write_text("# idx\n\n## digest退避（時系列索引）\n- 既存行\n\n## 他の節\n- x\n",
                                    encoding="utf-8")
        (root / "doc-budget.json").write_text(FIX_CFG, encoding="utf-8")

        # 終端規則が check_doc_budget と一致すること（規則の正典は1つ＝drift の機械検出）
        pat = r"^## 20\d\d-"
        mine = [k for k, _h, _s, _e in date_sections(FIXTURE, pat)[1]]
        theirs = [k for k, _h, _b, _l in split_sections(FIXTURE, pat)]
        eq("節キーが check_doc_budget.split_sections と一致", mine, theirs)

        # 機械タグは純関数＝ホスト名を注入して境界（記号・空・長さ）を直接見る
        eq("ホスト名の正規化（記号→-・小文字・空はlocal）",
           (machine_tag("PC-Alpha.local"), machine_tag("かい_PC"), machine_tag("")),
           ("pc-alpha-local", "pc", "local"))

        eq("dry-run は書き換えない",
           (rotate(root, lay, SELFTEST_KEEP, "20260726", SELFTEST_TAG, False),
            (root / lay.target).read_text(encoding="utf-8") == FIXTURE), (0, True))

        eq("apply", rotate(root, lay, SELFTEST_KEEP, "20260726", SELFTEST_TAG, True), 0)
        dig = (root / lay.target).read_text(encoding="utf-8")
        arc_rel = f"{lay.archive_dir}/session-digest-rotated-20260726-{SELFTEST_TAG}.md"
        eq("ファイル名に機械タグが入る（無印は作らない）",
           ((root / arc_rel).is_file(),
            (root / f"{lay.archive_dir}/session-digest-rotated-20260726.md").is_file()),
           (True, False))
        arc = (root / arc_rel).read_text(encoding="utf-8")
        idx = (root / idx_rel).read_text(encoding="utf-8")
        eq("古い2節が本体から消えた", ("2026-01-01" in dig, "2026-01-02" in dig), (False, False))
        eq("新しい2節は残る", ("2026-01-03" in dig and "2026-01-04" in dig), True)
        eq("対象外の通常節を巻き添えにしない", ("keep me" in dig and "tail" in dig), True)
        eq("archive に本文が逐語で入る", ("AAA" in arc and "BBB" in arc, "CCC" in arc), (True, False))
        eq("INDEX へ2行追記・既存行は保持", (idx.count("退避=[session-digest"), "既存行" in idx), (2, True))
        eq("INDEX の後続節を壊さない", idx.rstrip().endswith("- x"), True)

        # 冪等性: 残り2節 ≤ keep なので2回目は何もしない
        eq("2回目は no-op", (rotate(root, lay, SELFTEST_KEEP, "20260726", SELFTEST_TAG, True),
                            (root / lay.target).read_text(encoding="utf-8") == dig), (0, True))

        # 同一キーの再移送防止（keep=0 で残り2節を同じファイルへ回す＝本文重複しない）
        rotate(root, lay, 0, "20260726", SELFTEST_TAG, True)
        arc2 = (root / arc_rel).read_text(encoding="utf-8")
        eq("同名キーは本文を二重に書かない", arc2.count("AAA"), 1)
        eq("追加分の本文は入る", "CCC" in arc2, True)

        # 対象節ゼロのファイルで落ちない
        (root / lay.target).write_text("# d\n\n## 通常節\nx\n", encoding="utf-8")
        eq("対象節ゼロ=正常終了", rotate(root, lay, SELFTEST_KEEP, "20260726", SELFTEST_TAG, True), 0)

        # section_rules 不在の設定で fail-fast
        (root / "doc-budget.json").write_text('{"budgets":{},"section_rules":{}}', encoding="utf-8")
        eq("設定不在=1で落ちる", rotate(root, lay, SELFTEST_KEEP, "20260726", SELFTEST_TAG, True), 1)

    print("RESULT:", "ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="完結した日付節を archive へ回転させる")
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help=f"回転させるファイル（既定 {DEFAULT_TARGET}。doc-budget.json の section_rules キーと一致させる）")
    ap.add_argument("--archive-dir", default=DEFAULT_ARCHIVE, help=f"移送先ディレクトリ（既定 {DEFAULT_ARCHIVE}）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help=f"残す新しい節の数（既定 {DEFAULT_KEEP}）")
    ap.add_argument("--tag", default=None, help="ファイル名の機械タグ（既定=ホスト名を正規化）")
    ap.add_argument("--apply", action="store_true", help="実際に移送する（既定は dry-run）")
    ap.add_argument("--date", default=None, help="移送先ファイル名の日付（既定=今日 YYYYMMDD）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    stamp = args.date or datetime.date.today().strftime("%Y%m%d")
    return rotate(args.root, layout_of(args.target, args.archive_dir), args.keep, stamp,
                  args.tag or machine_tag(), args.apply)


if __name__ == "__main__":
    sys.exit(main())
