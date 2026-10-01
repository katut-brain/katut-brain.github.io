#!/usr/bin/env python3
# test_note_gaps.py — note_gaps.py の回帰テスト。
#
# 本番の関数（note_gaps.find_gaps / main、select_targets.select）を、一時ディレクトリに
# 作った captures.json / reviews/*.html / Vault の Explore/bookmarks/ へ実際に通す
# （モックで差し替えない。例外経路の1本だけ find_gaps を差し替える）。
# 標準ライブラリの unittest のみ（test_select_targets.py と同じ流儀）。

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import note_gaps  # noqa: E402
import select_targets  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _card(rid, url, title="題名", vdesc="一言"):
    # 本番の reviews/*.html と同じ入れ子（.vcard > a.vlink > .vbody > .vtitle/.vdesc ＋ button）。
    return (
        '<div class="vcard">\n'
        '  <a class="vlink" href="%s" target="_blank" rel="noopener">\n'
        '    <div class="thumb"><span class="ph">X</span></div>\n'
        '    <div class="vbody"><div class="vtitle">%s</div><div class="vdesc">%s</div>'
        '<div class="vsaved">保存 9/29 08:16</div></div>\n'
        '  </a>\n'
        '  <button class="deepdive" onclick="openChat(this)" data-url="%s" '
        'data-title="%s" data-rid="%s">💬 AIと話す</button>\n'
        '</div>\n' % (url, title, vdesc, url, title, rid))


def _review(path, cards):
    _write(path, "<!doctype html><html><body><main>%s</main></body></html>\n" % "".join(cards))


def _note(vault, name, rid=None, body="本文"):
    fm = "---\ndate: 2026-09-01\ntags: [type/bookmark]\n"
    if rid is not None:
        fm += "raindrop_id: %s\n" % rid
    fm += "source: https://example.com/\nrelated: []\n---\n\n"
    _write(os.path.join(vault, "Explore", "bookmarks", name), fm + body + "\n\n## 関連ノート\n")


def _read_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def _filler(vault, n=note_gaps.MIN_VAULT_NOTES):
    """Vault が「信用できる」件数に届くよう、無関係なノートを並べる。"""
    for i in range(n):
        _note(vault, "rd-%d-filler-%d.md" % (9000000 + i, i), 9000000 + i)


class Fixture(object):
    def __init__(self, d):
        self.d = d
        self.reviews = os.path.join(d, "repo", "reviews")
        self.captures = os.path.join(d, "repo", "captures.json")
        self.vault = os.path.join(d, "vault")
        os.makedirs(self.reviews)
        _write(self.captures, "[]")

    def gaps(self, target, **kw):
        return note_gaps.find_gaps(target, self.vault, reviews_dir=self.reviews,
                                   captures_path=self.captures, **kw)


class TestHoleReproducedAndClosed(unittest.TestCase):
    def test_step8_ok_step85_missing_is_picked_next_night(self):
        """P-0 の終了条件そのもの。T 夜: 手順3が R を選ぶ → 手順8で reviews/T.html が
        公開される → 手順8.5 が走らず Vault に R のノートが無い。T+1 夜: 手順3は R を
        選ばない（これが穴）が、note_gaps は R を拾う。ノートができたら T+2 夜は0件。
        """
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            R = 1860000001
            url = "https://x.com/someone/status/2100000000000000001"
            _write(fx.captures, json.dumps([
                {"rid": R, "date": "2026-09-10", "source": url, "title": "元の題名",
                 "note": "やってみたい", "cluster": "ai", "tags": ["AI"]}], ensure_ascii=False))

            # T 夜の手順3: R が選ばれる。
            sel_t = select_targets.select("2026-09-10", captures_path=fx.captures,
                                          reviews_dir=fx.reviews,
                                          capture_index_path=os.path.join(d, "none.json"))
            self.assertEqual(sel_t["rids"], [R])

            # 手順8 成功（reviews/T.html に R のカード）・手順8.5 未実行（Vault に無い）。
            _review(os.path.join(fx.reviews, "2026-09-10.html"),
                    [_card(R, url, "カード題名", "一言&amp;要点")])

            # T+1 夜の手順3: 公開済みなので R は選ばれない（旧手順書の前提が崩れている点）。
            sel_t1 = select_targets.select("2026-09-11", captures_path=fx.captures,
                                           reviews_dir=fx.reviews,
                                           capture_index_path=os.path.join(d, "none.json"))
            self.assertNotIn(R, sel_t1["rids"])

            # T+1 夜の note_gaps: R を拾い、材料が揃っている。
            res = fx.gaps("2026-09-11")
            self.assertEqual(res["status"], "ok")
            self.assertEqual([r["rid"] for r in res["records"]], [R])
            rec = res["records"][0]
            self.assertEqual(rec["date"], "2026-09-10")
            self.assertEqual(rec["date_from"], "captures")
            self.assertEqual(rec["source"], url)
            self.assertEqual(rec["title"], "元の題名")
            self.assertEqual(rec["note"], "やってみたい")
            self.assertEqual(rec["review_date"], "2026-09-10")
            self.assertEqual(rec["vdesc"], "一言&要点")

            # ノートができたら次の夜は0件（収束する）。
            _note(fx.vault, "rd-%d-recovered.md" % R, R)
            self.assertEqual(fx.gaps("2026-09-12")["records"], [])


class TestTonightExcluded(unittest.TestCase):
    def test_tonights_review_is_not_counted(self):
        """手順7.5 の時点で今夜の reviews/<TARGET>.html は書かれているが未公開。
        そこにしか無いカードは今夜の通常経路が作るので数えない。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-20.html"),
                    [_card(1860000002, "https://example.com/a")])
            self.assertEqual(fx.gaps("2026-09-20")["records"], [])
            self.assertEqual([r["rid"] for r in fx.gaps("2026-09-21")["records"]],
                             [1860000002])

    def test_card_also_in_earlier_review_is_counted_on_target_night(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            card = _card(1860000003, "https://example.com/b")
            _review(os.path.join(fx.reviews, "2026-09-19.html"), [card])
            _review(os.path.join(fx.reviews, "2026-09-20.html"), [card])
            recs = fx.gaps("2026-09-20")["records"]
            self.assertEqual([(r["rid"], r["review_date"]) for r in recs],
                             [(1860000003, "2026-09-19")])

    def test_targets_file_rids_are_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000004, "https://example.com/c"),
                     _card(1860000005, "https://example.com/d")])
            tpath = os.path.join(d, "targets.json")
            _write(tpath, json.dumps({"rids": [1860000004], "records": []}))
            recs = fx.gaps("2026-09-20", targets_path=tpath)["records"]
            self.assertEqual([r["rid"] for r in recs], [1860000005])


class TestVaultFailClosed(unittest.TestCase):
    def test_missing_vault_selects_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000006, "https://example.com/e")])
            res = fx.gaps("2026-09-20")
            self.assertEqual(res["status"], "vault_unreadable")
            self.assertEqual(res["reason"], "no_dir")
            self.assertEqual(res["records"], [])
            res = note_gaps.find_gaps("2026-09-20", None, reviews_dir=fx.reviews,
                                      captures_path=fx.captures)
            self.assertEqual(res["status"], "vault_unreadable")
            self.assertEqual(res["reason"], "no_dir")

    def test_wrong_directory_levels_select_nothing(self):
        """手順書の <Vaultリポのディレクトリ> を間違えても（1つ上・1つ下）何も選ばない。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000006, "https://example.com/e")])
            for wrong in (d, os.path.join(fx.vault, "Explore"),
                          os.path.join(fx.vault, "Explore", "bookmarks")):
                res = note_gaps.find_gaps("2026-09-20", wrong, reviews_dir=fx.reviews,
                                          captures_path=fx.captures)
                self.assertEqual((res["status"], res["records"], res["vault_notes"]),
                                 ("vault_unreadable", [], 0), wrong)

    def test_too_few_notes_selects_nothing(self):
        """読み取りが途中で切れた等で件数が少ないときに、全部を「無い」と数えない。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault, note_gaps.MIN_VAULT_NOTES - 1)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000007, "https://example.com/f")])
            res = fx.gaps("2026-09-20")
            self.assertEqual(res["status"], "vault_unreadable")
            self.assertEqual(res["reason"], "too_few:%d" % (note_gaps.MIN_VAULT_NOTES - 1))
            self.assertEqual(res["records"], [])
            _note(fx.vault, "rd-9999999-one-more.md", 9999999)
            self.assertEqual([r["rid"] for r in fx.gaps("2026-09-20")["records"]],
                             [1860000007])

    def test_threshold_value_is_pinned(self):
        """しきい値そのものを固定する（1 や 101 に変えられても他のテストは境界を
        note_gaps.MIN_VAULT_NOTES から作るので気づけない）。"""
        self.assertEqual(note_gaps.MIN_VAULT_NOTES, 100)

    def test_unreadable_note_without_rid_in_filename_selects_nothing(self):
        """rid をファイル名から取れず、中身も読めないノートがあると、その中に載っている
        rid を「無い」と数えかねない。100件以上あっても何も選ばない（読めた分は
        fail-closed にしない＝次のテストで確認）。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            bad = os.path.join(fx.vault, "Explore", "bookmarks", "rd-renamed-note.md")
            with io.open(bad, "wb") as f:
                f.write(b"---\nraindrop_id: 1860000070\n\xff\xfe broken bytes\n---\n")
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000070, "https://example.com/s")])
            res = fx.gaps("2026-09-20")
            self.assertEqual(res["status"], "vault_unreadable")
            self.assertEqual(res["reason"], "read_failed:1")
            self.assertEqual(res["records"], [])

    def test_unreadable_note_with_rid_in_filename_is_still_counted(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            bad = os.path.join(fx.vault, "Explore", "bookmarks", "rd-1860000071-broken.md")
            with io.open(bad, "wb") as f:
                f.write(b"\xff\xfe not utf-8")
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000071, "https://example.com/t"),
                     _card(1860000072, "https://example.com/u")])
            res = fx.gaps("2026-09-20")
            self.assertEqual(res["status"], "ok")
            self.assertEqual([r["rid"] for r in res["records"]], [1860000072])

    def test_directory_walk_failure_selects_nothing(self):
        """フォルダの走査に失敗したとき（権限等）は、見えなかった側に rid があるかも
        しれないので何も選ばない。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000073, "https://example.com/v")])
            real_walk = os.walk

            def failing_walk(top, onerror=None, **kw):
                if onerror is not None:
                    onerror(PermissionError("denied"))
                return real_walk(top, onerror=onerror, **kw)

            with unittest.mock.patch.object(note_gaps.os, "walk", side_effect=failing_walk):
                res = fx.gaps("2026-09-20")
            self.assertEqual(res["status"], "vault_unreadable")
            self.assertEqual(res["reason"], "read_failed:1")
            self.assertEqual(res["records"], [])


class TestVaultNoteDetection(unittest.TestCase):
    def test_filename_rid_counts_even_if_frontmatter_broken(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _write(os.path.join(fx.vault, "Explore", "bookmarks",
                                "rd-1860000008-broken.md"), "frontmatter が無い\n")
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000008, "https://example.com/g")])
            self.assertEqual(fx.gaps("2026-09-20")["records"], [])

    def test_frontmatter_rid_counts_even_if_filename_differs(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _note(fx.vault, "rd-renamed-note.md", 1860000009)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000009, "https://example.com/h")])
            self.assertEqual(fx.gaps("2026-09-20")["records"], [])

    def test_notes_in_subfolders_and_other_names_are_counted(self):
        """手順7.5 の重複チェックは grep -r（サブフォルダも・ファイル名不問）。ここが
        非再帰だと、そのノートを毎晩 gap と数えては重複チェックで飛ばし、古い順の
        5枠を占有して、新しい本物の gap が回収されなくなる。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            bm = os.path.join(fx.vault, "Explore", "bookmarks")
            _note(fx.vault, os.path.join("archive", "old-1.md"), 1860000080)
            _note(fx.vault, os.path.join("a", "b", "nested-2.md"), 1860000081)
            _note(fx.vault, "manual-3.md", 1860000082)
            _write(os.path.join(bm, "archive", "rd-1860000083-deep.md"), "中身なし\n")
            _review(os.path.join(fx.reviews, "2026-09-10.html"),
                    [_card(1860000080 + i, "https://example.com/w%d" % i) for i in range(4)])
            _review(os.path.join(fx.reviews, "2026-09-20.html"),
                    [_card(1860000099, "https://example.com/x")])
            res = fx.gaps("2026-09-25")
            self.assertEqual([r["rid"] for r in res["records"]], [1860000099])

    def test_filename_and_frontmatter_variants(self):
        """BOM 付き・行末コメント付き・slug の無い rd-<rid>.md も「ある」と数える
        （数え落とすと Vault に重複ノートができる）。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            bm = os.path.join(fx.vault, "Explore", "bookmarks")
            with io.open(os.path.join(bm, "bom.md"), "wb") as f:
                f.write("﻿---\nraindrop_id: 1860000090\n---\n".encode("utf-8"))
            _write(os.path.join(bm, "comment.md"),
                   "---\nraindrop_id: 1860000091   # 手で直した\n---\n")
            _write(os.path.join(bm, "quoted.md"), "---\r\nraindrop_id: \"1860000092\"\r\n---\r\n")
            _write(os.path.join(bm, "rd-1860000093.md"), "slug 無し\n")
            _write(os.path.join(bm, "body-only.md"),
                   "---\ntags: []\n---\n本文に raindrop_id: 1860000094 と書いただけ\n")
            _review(os.path.join(fx.reviews, "2026-09-10.html"),
                    [_card(1860000090 + i, "https://example.com/y%d" % i) for i in range(5)])
            res = fx.gaps("2026-09-25")
            self.assertEqual([r["rid"] for r in res["records"]], [1860000094])

    def test_negative_synthetic_rid(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(-123456, "https://example.com/i"),
                     _card(-654321, "https://example.com/j")])
            _note(fx.vault, "rd--123456-ridless.md", -123456)
            self.assertEqual([r["rid"] for r in fx.gaps("2026-09-20")["records"]],
                             [-654321])


class TestSinceAndLimit(unittest.TestCase):
    def test_before_since_is_not_counted(self):
        """1ブックマーク1ノート制より前に公開された分（最新 08-03）は数えない。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-08-03.html"),
                    [_card(1809034085, "https://example.com/k")])
            _review(os.path.join(fx.reviews, "2026-08-04.html"),
                    [_card(1809100000, "https://example.com/l")])
            _review(os.path.join(fx.reviews, "2026-08-05.html"),
                    [_card(1809200000, "https://example.com/m")])
            res = fx.gaps("2026-09-20")
            self.assertEqual(note_gaps.GAP_SINCE, "2026-08-05")
            self.assertEqual([r["rid"] for r in res["records"]], [1809200000])
            self.assertEqual(res["gaps"], 1)

    def test_limit_takes_oldest_first_and_reports_total(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-12.html"),
                    [_card(1860000020, "https://example.com/n1"),
                     _card(1860000010, "https://example.com/n2")])
            _review(os.path.join(fx.reviews, "2026-09-11.html"),
                    [_card(1860000030, "https://example.com/n3")])
            _review(os.path.join(fx.reviews, "2026-09-13.html"),
                    [_card(1860000040 + i, "https://example.com/o%d" % i) for i in range(4)])
            res = fx.gaps("2026-09-20")
            self.assertEqual(res["gaps"], 7)
            self.assertEqual(note_gaps.DEFAULT_LIMIT, 5)
            self.assertEqual([r["rid"] for r in res["records"]],
                             [1860000030, 1860000010, 1860000020, 1860000040, 1860000041])
            self.assertEqual(fx.gaps("2026-09-20", limit=0)["records"], [])


class TestMaterial(unittest.TestCase):
    def test_string_rid_in_captures_and_card_only_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _write(fx.captures, json.dumps([
                {"rid": "1860000050", "date": "2026-09-01", "source": "https://example.com/p",
                 "title": "記録の題名", "note": "", "cluster": "arch", "tags": ["建築"]}],
                ensure_ascii=False))
            _review(os.path.join(fx.reviews, "2026-09-02.html"),
                    [_card(1860000050, "https://example.com/p"),
                     _card(1860000051, "https://example.com/q?a=1&amp;b=2", "カードだけ", "一言")])
            recs = {r["rid"]: r for r in fx.gaps("2026-09-20")["records"]}
            self.assertEqual(recs[1860000050]["date_from"], "captures")
            self.assertEqual(recs[1860000050]["date"], "2026-09-01")
            self.assertEqual(recs[1860000050]["title"], "記録の題名")
            self.assertEqual(recs[1860000050]["cluster"], "arch")
            fb = recs[1860000051]
            self.assertEqual(fb["date_from"], "review")
            self.assertEqual(fb["date"], "2026-09-02")
            self.assertEqual(fb["source"], "https://example.com/q?a=1&b=2")
            self.assertEqual(fb["title"], "カードだけ")
            self.assertEqual(fb["vdesc"], "一言")


class TestCli(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = note_gaps.main(argv)
        return rc, buf.getvalue()

    def test_out_is_written_and_status_line(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000060, "https://example.com/r")])
            out = os.path.join(d, "gaps.json")
            rc, text = self._run(["--target", "2026-09-20", "--vault-dir", fx.vault,
                                  "--reviews-dir", fx.reviews, "--captures", fx.captures,
                                  "--out", out])
            self.assertEqual(rc, 0)
            self.assertIn("NOTE_GAPS: target=2026-09-20 status=ok gaps=1 selected=1 ", text)
            data = _read_json(out)
            self.assertEqual([r["rid"] for r in data["records"]], [1860000060])

    def test_unreadable_vault_still_writes_empty_out(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            out = os.path.join(d, "gaps.json")
            rc, text = self._run(["--target", "2026-09-20", "--vault-dir",
                                  os.path.join(d, "nope"), "--reviews-dir", fx.reviews,
                                  "--captures", fx.captures, "--out", out])
            self.assertEqual(rc, 0)
            self.assertIn("status=vault_unreadable gaps=0 selected=0", text)
            self.assertEqual(_read_json(out)["records"], [])

    def test_args_are_wired_through_main(self):
        """手順書は CLI で呼ぶ。--targets / --limit / --since が main から find_gaps へ
        本当に渡っていること（find_gaps を直接呼ぶテストでは配線を壊しても緑になる）。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-08-20.html"),
                    [_card(1860000100, "https://example.com/c1")])
            _review(os.path.join(fx.reviews, "2026-09-10.html"),
                    [_card(1860000101, "https://example.com/c2"),
                     _card(1860000102, "https://example.com/c3"),
                     _card(1860000103, "https://example.com/c4")])
            tpath = os.path.join(d, "targets.json")
            _write(tpath, json.dumps({"rids": [1860000101]}))
            out = os.path.join(d, "gaps.json")
            rc, text = self._run(["--target", "2026-09-20", "--vault-dir", fx.vault,
                                  "--reviews-dir", fx.reviews, "--captures", fx.captures,
                                  "--targets", tpath, "--limit", "1", "--since", "2026-09-01",
                                  "--out", out])
            self.assertEqual(rc, 0)
            # 08-20 は --since より前・1860000101 は --targets で除外 → 残り2件のうち limit=1。
            self.assertIn("status=ok gaps=2 selected=1 ", text)
            self.assertIn("since=2026-09-01 limit=1", text)
            self.assertEqual([r["rid"] for r in _read_json(out)["records"]], [1860000102])

    def test_malformed_target_falls_back_to_yesterday(self):
        """TARGET_OVERRIDE の打ち間違いをそのまま使うと、今夜の reviews を除外できない。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            rc, text = self._run(["--target", "garbage", "--vault-dir", fx.vault,
                                  "--reviews-dir", fx.reviews, "--captures", fx.captures])
            self.assertEqual(rc, 0)
            self.assertIn("target=%s status=ok" % select_targets._default_target(), text)
            self.assertNotIn("garbage", text)

    def test_unwritable_out_is_not_reported_ok_and_old_out_is_removed(self):
        """--out に書けなかったのに status=ok のままだと、前の夜の /tmp/note_gaps.json が
        残っていたときに古い records をノートにしてしまう。"""
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            _review(os.path.join(fx.reviews, "2026-09-19.html"),
                    [_card(1860000110, "https://example.com/z")])
            out = os.path.join(d, "gaps.json")
            _write(out, json.dumps({"status": "ok", "records": [{"rid": 1}]}))
            os.makedirs(out + ".tmp")  # 一時ファイルを作れない状態にする
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                rc, text = self._run(["--target", "2026-09-20", "--vault-dir", fx.vault,
                                      "--reviews-dir", fx.reviews, "--captures", fx.captures,
                                      "--out", out])
            self.assertEqual(rc, 0)
            self.assertIn("status=error gaps=1 selected=0 ", text)
            self.assertIn("error=out_", text)
            self.assertFalse(os.path.exists(out))

    def test_missing_out_parent_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            _filler(fx.vault)
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                rc, text = self._run(["--target", "2026-09-20", "--vault-dir", fx.vault,
                                      "--reviews-dir", fx.reviews, "--captures", fx.captures,
                                      "--out", os.path.join(d, "no", "such", "dir", "g.json")])
            self.assertEqual(rc, 0)
            self.assertIn("status=error", text)
            self.assertIn("error=out_", text)

    def test_bad_arguments_still_print_a_status_line_and_exit_zero(self):
        """argparse は不正な引数で SystemExit(2) を投げる。行が出ないと無人エージェントは
        status を判定できない。"""
        for argv in (["--bogus", "1"], ["--limit", "abc"], ["--vault-dir"],
                     ["--vault-dir", "/tmp/my", "vault"]):
            with unittest.mock.patch("sys.stderr", io.StringIO()):
                rc, text = self._run(argv)
            self.assertEqual(rc, 0, argv)
            self.assertIn("NOTE_GAPS: target=unknown status=error", text)
            self.assertIn("error=bad_args", text)

    def test_unreadable_vault_reports_reason(self):
        with tempfile.TemporaryDirectory() as d:
            fx = Fixture(d)
            rc, text = self._run(["--target", "2026-09-20", "--vault-dir", d,
                                  "--reviews-dir", fx.reviews, "--captures", fx.captures])
            self.assertIn("status=vault_unreadable gaps=0 selected=0 vault_notes=0 ", text)
            self.assertIn("reason=no_dir", text)

    def test_exception_still_exits_zero_with_empty_out(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "gaps.json")
            with unittest.mock.patch.object(note_gaps, "find_gaps",
                                            side_effect=RuntimeError("boom")):
                buf = io.StringIO()
                with redirect_stdout(buf), unittest.mock.patch("sys.stderr", io.StringIO()):
                    rc = note_gaps.main(["--target", "2026-09-20", "--vault-dir", d,
                                         "--out", out])
            self.assertEqual(rc, 0)
            self.assertIn("status=error", buf.getvalue())
            self.assertIn("error=RuntimeError", buf.getvalue())
            self.assertEqual(_read_json(out)["records"], [])


if __name__ == "__main__":
    unittest.main()
