#!/usr/bin/env python3
"""汎用監視エンジンのテスト。標準ライブラリのみ。

  python -m unittest discover -s tests

監視でいちばん怖い壊れ方は「誤爆」と「黙って死ぬ」の2つなので、
そこに関わる分岐（sanity・取りこぼし・クールダウン・初回実行）を
重点的に固めてある。
"""

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import conditions, core, http, notify, state  # noqa: E402
from watcher.extract import json_path, json_pick, regex_items  # noqa: E402
from watcher.sources import json_api, rakuten_travel  # noqa: E402
from watcher.util import JST, parse_duration, to_number  # noqa: E402

import run as runner  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestUtil(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("6h"), 21600)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration(120), 120)

    def test_parse_duration_accepts_bare_numbers(self):
        # "0"（抑制なし）を書けないと watches.json が実用に耐えない
        self.assertEqual(parse_duration("0"), 0)
        self.assertEqual(parse_duration("600"), 600)

    def test_parse_duration_falls_back_on_blank(self):
        # workflow_dispatch の未入力は空文字で来るので既定値に戻す必要がある
        self.assertEqual(parse_duration("", 99), 99)
        self.assertEqual(parse_duration(None, 99), 99)
        with self.assertRaises(ValueError):
            parse_duration("10分")

    def test_to_number(self):
        self.assertEqual(to_number("20,000円"), 20000.0)
        self.assertEqual(to_number("3.5"), 3.5)
        self.assertEqual(to_number(1500), 1500.0)

    def test_to_number_returns_none_when_unparsable(self):
        for v in ("在庫なし", "－", "", None):
            self.assertIsNone(to_number(v), v)


class TestExtract(unittest.TestCase):
    data = {"hotels": [
        {"hotel": [{"info": {"no": 1, "name": "A"}},
                   {"rooms": [{"total": 12000}, {"total": 8000}]}]},
        {"hotel": [{"info": {"no": 2, "name": "B"}},
                   {"rooms": [{"total": 30000}]}]},
    ]}

    def test_wildcard_and_index(self):
        self.assertEqual(
            json_path(self.data, "hotels[*].hotel[0].info.name"), ["A", "B"])
        self.assertEqual(
            json_path(self.data, "hotels[0].hotel[1].rooms[*].total"),
            [12000, 8000])
        self.assertEqual(
            json_path(self.data, "hotels[-1].hotel[0].info.name"), ["B"])
        self.assertEqual(json_pick(self.data, "hotels[1].hotel[0].info.no"), 2)

    def test_missing_branches_are_dropped_not_raised(self):
        # API が項目を省略してくるのは普通なので例外にしない
        self.assertEqual(json_path(self.data, "hotels[*].missing.deep"), [])
        self.assertEqual(json_path(self.data, "hotels[9].hotel"), [])

    def test_bad_path_raises(self):
        for bad in ("a..b", "a[x]", "a[0"):
            with self.assertRaises(ValueError, msg=bad):
                json_path(self.data, bad)

    def test_regex_named_groups_become_fields(self):
        html = ('<li data-sku="X1"><b>在庫あり</b><span class="p">1,980円</span>'
                '</li><li data-sku="X2"><b>入荷待ち</b>'
                '<span class="p">2,480円</span></li>')
        pattern = (r'data-sku="(?P<id>[^"]+)">\s*<b>(?P<status>[^<]+)</b>'
                   r'\s*<span class="p">(?P<price>[^<]+)</span>')
        items = regex_items(html, pattern, "s")
        self.assertEqual([i["id"] for i in items], ["X1", "X2"])
        self.assertEqual(items[0]["status"], "在庫あり")

    def test_pattern_without_names_is_rejected(self):
        with self.assertRaises(ValueError):
            regex_items("<a>x</a>", r"<a>(.*?)</a>")


class TestConditions(unittest.TestCase):
    def test_appeared_notifies_only_new(self):
        items = [{"id": "a"}, {"id": "b"}]
        fresh, active, _ = conditions.evaluate(
            {"type": "appeared"}, items, {"active": ["a"]})
        self.assertEqual([k for k, _, _ in fresh], ["b"])
        self.assertEqual(sorted(active), ["a", "b"])

    def test_threshold_refires_after_condition_breaks(self):
        """成立中は黙り、崩れて再成立したらまた通知する。

        「下がった→戻った→また下がった」を取りこぼさないために必要。
        """
        cond = {"type": "threshold", "field": "price", "op": "<=",
                "value": 20000}
        low = [{"id": "h1", "price": "18,000円"}]
        high = [{"id": "h1", "price": "25,000円"}]

        fresh, active, _ = conditions.evaluate(cond, low, {})
        self.assertEqual(len(fresh), 1)
        self.assertIn("price 18000 <= 20000", fresh[0][2])

        fresh, active, _ = conditions.evaluate(cond, low, {"active": active})
        self.assertEqual(fresh, [], "成立中に再通知してはいけない")

        fresh, active, _ = conditions.evaluate(cond, high, {"active": active})
        self.assertEqual((fresh, active), ([], []))

        fresh, _, _ = conditions.evaluate(cond, low, {"active": active})
        self.assertEqual(len(fresh), 1, "再成立したら通知すべき")

    def test_unparsable_value_never_satisfies(self):
        """値が取れないものを成立させると、抽出が壊れた瞬間に全件通知が飛ぶ。"""
        cond = {"type": "threshold", "field": "price", "op": "<=",
                "value": 20000}
        fresh, active, _ = conditions.evaluate(
            cond, [{"id": "h1", "price": "－"}], {})
        self.assertEqual((fresh, active), ([], []))

    def test_contains_and_negation(self):
        fresh, _, _ = conditions.evaluate(
            {"type": "contains", "field": "s", "value": "在庫あり"},
            [{"id": "x", "s": "在庫あり"}, {"id": "y", "s": "入荷待ち"}], {})
        self.assertEqual([k for k, _, _ in fresh], ["x"])

        fresh, _, _ = conditions.evaluate(
            {"type": "not_contains", "field": "s", "value": "SOLD OUT"},
            [{"id": "x", "s": "残りわずか"}, {"id": "y", "s": "SOLD OUT"}], {})
        self.assertEqual([k for k, _, _ in fresh], ["x"])

    def test_all_composition(self):
        cond = {"type": "all", "conditions": [
            {"type": "contains", "field": "s", "value": "在庫あり"},
            {"type": "threshold", "field": "price", "op": "<=",
             "value": 2000}]}
        fresh, _, _ = conditions.evaluate(cond, [
            {"id": "x", "s": "在庫あり", "price": "1,980円"},
            {"id": "y", "s": "在庫あり", "price": "2,480円"},
            {"id": "z", "s": "在庫なし", "price": "980円"}], {})
        self.assertEqual([k for k, _, _ in fresh], ["x"])

    def test_changed_reports_both_values(self):
        fresh, _, _ = conditions.evaluate(
            {"type": "changed", "field": "price"},
            [{"id": "x", "price": "100"}], {"values": {"x": {"price": "90"}}})
        self.assertIn("90 → 100", fresh[0][2])

    def test_gone_detects_disappearance(self):
        fresh, _, _ = conditions.evaluate(
            {"type": "gone"}, [{"id": "a"}],
            {"seen": ["a", "b"], "values": {"b": {"label": "B"}}})
        self.assertEqual([k for k, _, _ in fresh], ["b"])

    def test_key_is_stable_without_id(self):
        """キーが毎回変わるソースだと通知が止まらなくなる。"""
        self.assertEqual(conditions._key({"label": "X"}), "X")
        self.assertEqual(conditions._key({"price": "1", "status": "2"}),
                         "price=1|status=2")

    def test_bad_conditions_raise(self):
        for bad in ({"type": "nope"},
                    {"type": "threshold", "field": "p", "op": "=~",
                     "value": 1},
                    {"type": "threshold", "field": "p", "value": "なし"},
                    {"type": "all"}):
            with self.assertRaises(conditions.ConditionError, msg=str(bad)):
                conditions.evaluate(bad, [{"id": "a", "p": "1"}], {})


class FakeHTTP:
    """http.fetch を差し替えるための記録つきフェイク。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def __call__(self, url, data=None, **kw):
        self.calls.append({"url": url, "data": data, **kw})
        return self.responder(url, data, kw)


class TestJsonApi(unittest.TestCase):
    def setUp(self):
        real = http.fetch
        self.addCleanup(lambda: setattr(http, "fetch", real))

    def recipe(self, **over):
        cfg = {"url": "https://x.test/a", "data": {"q": "1"}, "items": "[*]",
               "fields": {"id": "id", "label": "name"},
               "paginate": {"param": "pageIndex",
                            "total_path": "[0].allCount"}}
        cfg.update(over)
        return cfg

    def test_pagination_collects_all_pages(self):
        pages = {
            "0": [{"allCount": 7, "rowMax": 5, "id": "r%d" % i,
                   "name": "部屋%d" % i} for i in range(5)],
            "1": [{"allCount": 7, "rowMax": 5, "id": "r%d" % i,
                   "name": "部屋%d" % i} for i in range(5, 7)],
        }
        fake = FakeHTTP(lambda u, d, k: json.dumps(pages[d["pageIndex"]]))
        http.fetch = fake
        items = json_api.fetch(self.recipe())
        self.assertEqual(len(items), 7)
        self.assertEqual([c["data"]["pageIndex"] for c in fake.calls],
                         ["0", "1"])

    def test_truncated_result_is_an_error_not_a_short_list(self):
        """全件取れていないのに成功させると、取りこぼしが消失扱いになる。"""
        page0 = [{"allCount": 7, "rowMax": 5, "id": "r%d" % i}
                 for i in range(5)]
        http.fetch = FakeHTTP(
            lambda u, d, k: json.dumps(page0) if d["pageIndex"] == "0"
            else "null")
        with self.assertRaises(http.SanityError) as cm:
            json_api.fetch(self.recipe())
        self.assertIn("7件のうち5件", str(cm.exception))

    def test_max_pages_guard_is_an_error(self):
        http.fetch = FakeHTTP(lambda u, d, k: json.dumps(
            [{"allCount": 999, "rowMax": 5, "id": "p" + d["pageIndex"]}]))
        with self.assertRaises(http.SanityError):
            json_api.fetch(self.recipe(
                paginate={"param": "pageIndex", "total_path": "[0].allCount",
                          "max_pages": 3}))

    def test_null_body_is_empty_not_failure(self):
        """UR は空室0件のとき本文そのものが null。失敗ではない。"""
        http.fetch = FakeHTTP(lambda u, d, k: "null")
        self.assertEqual(json_api.fetch(self.recipe()), [])

    def test_sanity_path_guards_structure_change(self):
        http.fetch = FakeHTTP(lambda u, d, k: json.dumps({"unexpected": 1}))
        with self.assertRaises(http.SanityError):
            json_api.fetch({"url": "https://x.test/a", "items": "rooms[*]",
                            "fields": {"id": "id"}, "sanity_path": "status"})

    def test_relative_link_and_entity_are_normalized(self):
        http.fetch = FakeHTTP(lambda u, d, k: json.dumps(
            [{"id": "1", "link": "/chintai/x.html", "space": "42&#13217;"}]))
        got = json_api.fetch({
            "url": "https://x.test/a", "items": "[*]",
            "fields": {"id": "id", "link": "link", "space": "space"},
            "link_prefix": "https://www.ur-net.go.jp"})
        self.assertEqual(got[0]["link"],
                         "https://www.ur-net.go.jp/chintai/x.html")
        self.assertEqual(got[0]["space"], "42㎡")

    def test_non_json_body_is_sanity_error(self):
        http.fetch = FakeHTTP(lambda u, d, k: "<html>メンテナンス中</html>")
        with self.assertRaises(http.SanityError):
            json_api.fetch(self.recipe())


class TestRakutenTravel(unittest.TestCase):
    def setUp(self):
        real = http.fetch
        self.addCleanup(lambda: setattr(http, "fetch", real))
        self.cfg = {"hotel_no": "1", "checkin": "2026-11-03",
                    "checkout": "2026-11-04", "application_id": "dummy"}

    def test_pairs_room_and_charge_regardless_of_order(self):
        sample = {"hotels": [{"hotel": [
            {"hotelBasicInfo": {"hotelName": "テスト旅館",
                                "hotelInformationUrl": "https://h.test/1"}},
            {"roomInfo": [
                {"dailyCharge": {"total": 18000}},
                {"roomBasicInfo": {"planId": "p1", "planName": "素泊まり",
                                   "roomName": "和室",
                                   "reserveUrl": "https://h.test/r1"}}]},
            {"roomInfo": [
                {"roomBasicInfo": {"planId": "p2", "planName": "2食付",
                                   "roomName": "洋室"}},
                {"dailyCharge": {"total": 25000}}]},
            {"roomInfo": [{"roomBasicInfo": {"planId": "p3"}},
                          {"dailyCharge": {}}]},
        ]}]}
        http.fetch = FakeHTTP(lambda u, d, k: json.dumps(sample))
        items = rakuten_travel.fetch(self.cfg)
        self.assertEqual([i["id"] for i in items], ["p1", "p2"],
                         "価格の無いプランは落とす")
        self.assertEqual(items[0]["label"], "テスト旅館 / 和室 / 素泊まり")
        self.assertEqual(items[0]["url"], "https://h.test/r1")
        self.assertEqual(items[1]["url"], "https://h.test/1")

    def test_not_found_means_no_vacancy_not_failure(self):
        """楽天は空室0件のとき 404 + not_found を返す。⚠️ を飛ばしてはいけない。"""
        http.fetch = FakeHTTP(
            lambda u, d, k: '{"error":"not_found","error_description":"x"}')
        self.assertEqual(rakuten_travel.fetch(self.cfg), [])

    def test_other_api_errors_do_fail(self):
        http.fetch = FakeHTTP(
            lambda u, d, k:
            '{"error":"wrong_parameter","error_description":"b"}')
        with self.assertRaises(http.FetchError) as cm:
            rakuten_travel.fetch(self.cfg)
        self.assertIn("wrong_parameter", str(cm.exception))

    def test_404_is_allowed_through_to_the_body(self):
        """allow_status を渡さないと 404 が FetchError になって毎回誤報する。"""
        seen = {}

        def responder(u, d, k):
            seen.update(k)
            return '{"error":"not_found"}'

        http.fetch = FakeHTTP(responder)
        rakuten_travel.fetch(self.cfg)
        self.assertIn(404, seen.get("allow_status", ()))

    def test_missing_app_id_is_a_config_error(self):
        import os
        saved = os.environ.pop("RAKUTEN_APP_ID", None)
        if saved:
            self.addCleanup(os.environ.__setitem__, "RAKUTEN_APP_ID", saved)
        cfg = dict(self.cfg)
        cfg.pop("application_id")
        with self.assertRaises(ValueError):
            rakuten_travel.fetch(cfg)


class CoreTestCase(unittest.TestCase):
    """core.run_watch の挙動。ソースと通知を差し替えて検証する。"""

    def setUp(self):
        from watcher import sources

        self.sent = []
        self.ok = True
        real_push = notify.line_push
        notify.line_push = self._push
        self.addCleanup(lambda: setattr(notify, "line_push", real_push))

        self.items = [{"id": "a", "label": "A", "price": "1000"}]
        self.error = None
        sources.REGISTRY["fake"] = self._fake
        self.addCleanup(lambda: sources.REGISTRY.pop("fake", None))

        self.now = datetime.datetime(2026, 10, 7, 12, 0, tzinfo=JST)

    def _push(self, msg):
        self.sent.append(msg)
        return self.ok

    def _fake(self, cfg):
        if self.error:
            raise self.error
        return self.items

    def watch(self, **over):
        w = {"id": "w1", "name": "テスト", "source": {"type": "fake"},
             "condition": {"type": "appeared"}, "notify": {"cooldown": "0"}}
        w.update(over)
        return w

    def at(self, minutes):
        return self.now + datetime.timedelta(minutes=minutes)


class TestCore(CoreTestCase):
    def test_first_run_summarizes_instead_of_flooding(self):
        self.items = [{"id": str(i), "label": "部屋%d" % i}
                      for i in range(30)]
        st = {}
        r = core.run_watch(self.watch(), st, self.now)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["first_run"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("監視を開始しました", self.sent[0])
        self.assertEqual(r["fresh"], [], "初回は全件を新着として流さない")
        self.assertEqual(len(st["active"]), 30)

    def test_second_run_notifies_only_new_items(self):
        st = {}
        core.run_watch(self.watch(), st, self.now)
        self.sent.clear()
        self.items = self.items + [{"id": "b", "label": "B"}]
        r = core.run_watch(self.watch(), st, self.at(20))
        self.assertEqual([i["id"] for i, _ in r["fresh"]], ["b"])
        self.assertIn("B", self.sent[0])

    def test_interval_is_respected(self):
        st = {}
        core.run_watch(self.watch(interval="1h"), st, self.now)
        r = core.run_watch(self.watch(interval="1h"), st, self.at(10))
        self.assertEqual(r["status"], "skipped")
        r = core.run_watch(self.watch(interval="1h"), st, self.at(60))
        self.assertEqual(r["status"], "ok")

    def test_cooldown_suppresses_repeat_notification(self):
        """境界を行き来する値で10分ごとに通知が飛ぶのを防ぐ。"""
        cond = {"type": "threshold", "field": "price", "op": "<=",
                "value": 2000}
        w = self.watch(condition=cond, notify={"cooldown": "6h"})
        st = {}
        core.run_watch(w, st, self.now)
        self.sent.clear()

        self.items = [{"id": "a", "label": "A", "price": "9000"}]
        core.run_watch(w, st, self.at(20))     # 条件が崩れる
        self.items = [{"id": "a", "label": "A", "price": "1000"}]
        core.run_watch(w, st, self.at(40))     # 再成立 → 通知
        self.assertEqual(len(self.sent), 1)

        self.items = [{"id": "a", "label": "A", "price": "9000"}]
        core.run_watch(w, st, self.at(60))
        self.items = [{"id": "a", "label": "A", "price": "1000"}]
        core.run_watch(w, st, self.at(80))     # クールダウン中
        self.assertEqual(len(self.sent), 1, "6時間以内の再通知は抑える")

    def test_suppressed_notification_is_dropped_not_deferred(self):
        """クールダウンで抑えたぶんは、明けてから配信し直さない。

        抑えた時点で条件は成立し続けているだけなので、6時間後に
        「まだ安いです」と送っても新しい情報が無い。値の推移そのものを
        追いたい場合は condition: changed を使う。
        """
        cond = {"type": "threshold", "field": "price", "op": "<=",
                "value": 2000}
        w = self.watch(condition=cond, notify={"cooldown": "6h"})
        st = {}
        core.run_watch(w, st, self.now)
        self.sent.clear()

        self.items = [{"id": "a", "label": "A", "price": "9000"}]
        core.run_watch(w, st, self.at(20))
        self.items = [{"id": "a", "label": "A", "price": "1000"}]
        core.run_watch(w, st, self.at(40))      # 通知される
        core.run_watch(w, st, self.at(60))      # 成立継続 → 黙る
        core.run_watch(w, st, self.at(10 * 60))  # クールダウン明けでも黙る
        self.assertEqual(len(self.sent), 1)

    def test_once_stops_the_watch_after_notifying(self):
        w = self.watch(notify={"once": True, "cooldown": "0"})
        st = {}
        core.run_watch(w, st, self.now)
        self.items = self.items + [{"id": "b", "label": "B"}]
        core.run_watch(w, st, self.at(20))
        self.assertTrue(st.get("stopped"))
        r = core.run_watch(w, st, self.at(40))
        self.assertEqual(r["status"], "stopped")

    def test_fetch_error_notifies_once_then_stays_quiet(self):
        self.error = http.FetchError("接続できません")
        st = {}
        r = core.run_watch(self.watch(), st, self.now)
        self.assertEqual(r["status"], "error")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("⚠️", self.sent[0])
        core.run_watch(self.watch(), st, self.at(20))
        self.assertEqual(len(self.sent), 1, "同じ障害を毎回通知しない")

    def test_recovery_allows_a_future_error_notification(self):
        self.error = http.FetchError("x")
        st = {}
        core.run_watch(self.watch(), st, self.now)
        self.error = None
        core.run_watch(self.watch(), st, self.at(20))
        self.assertNotIn("error_notified", st)

    def test_error_still_advances_the_clock_so_backoff_works(self):
        """失敗時に間隔を無視すると、弱っているサイトを最も強く叩く。"""
        self.error = http.FetchError("500")
        st = {}
        core.run_watch(self.watch(interval="1h"), st, self.now)
        r = core.run_watch(self.watch(interval="1h"), st, self.at(10))
        self.assertEqual(r["status"], "skipped")

    def test_sanity_error_is_treated_as_failure_not_emptiness(self):
        """構造変更を『空き無し』と誤認すると監視は黙ったまま死ぬ。"""
        st = {}
        core.run_watch(self.watch(), st, self.now)
        self.sent.clear()
        self.error = http.SanityError("ページ構成が変わりました")
        r = core.run_watch(self.watch(), st, self.at(20))
        self.assertEqual(r["status"], "error")
        self.assertIn("⚠️", self.sent[0])
        self.assertEqual(st["active"], ["a"], "成立集合を空にしてはいけない")

    def test_failed_notification_is_retried_next_run(self):
        st = {}
        core.run_watch(self.watch(), st, self.now)
        self.items = self.items + [{"id": "b", "label": "B"}]
        self.ok = False
        core.run_watch(self.watch(), st, self.at(20))
        self.assertNotIn("b", st["active"], "送れていないものは成立扱いにしない")
        self.ok = True
        r = core.run_watch(self.watch(), st, self.at(40))
        self.assertEqual([i["id"] for i, _ in r["fresh"]], ["b"])

    def test_misconfigured_watch_is_reported_not_notified(self):
        r = core.run_watch(self.watch(source={"type": "nonexistent"}), {},
                           self.now)
        self.assertEqual(r["status"], "misconfigured")
        self.assertEqual(self.sent, [], "設定ミスで LINE を飛ばさない")

    def test_disabled_watch_is_skipped(self):
        r = core.run_watch(self.watch(enabled=False), {}, self.now)
        self.assertEqual(r["status"], "disabled")


class TestRunner(unittest.TestCase):
    def _write(self, obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(obj, f)
            return f.name

    def test_duplicate_and_missing_ids_are_rejected(self):
        for bad in ({"watches": [{"id": "a"}, {"id": "a"}]},
                    {"watches": [{"name": "x"}]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                runner.load_config(self._write(bad))

    def test_shipped_config_is_valid(self):
        """watches.json が壊れていると cron の run が毎回落ちる。"""
        from watcher import sources

        cfg, watches = runner.load_config(str(ROOT / "watches.json"))
        self.assertTrue(watches)
        for w in watches:
            sources.get(w["source"]["type"])
            parse_duration(w.get("interval", "10m"))
            parse_duration(w.get("notify", {}).get("cooldown", "6h"))
            conditions.evaluate(w.get("condition"), [], {})

    def test_digest_is_sent_once_per_day_after_the_hour(self):
        now = datetime.datetime(2026, 10, 7, 19, 5, tzinfo=JST)
        self.assertFalse(runner.digest_due({}, now.replace(hour=18), 19))
        self.assertTrue(runner.digest_due({}, now, 19))
        self.assertFalse(runner.digest_due({"last": "2026-10-07"}, now, 19))
        self.assertTrue(runner.digest_due({"last": "2026-10-06"}, now, 19))

    def test_digest_lists_every_watch_status(self):
        results = [{"name": "A", "status": "ok", "items": 3, "active": 1},
                   {"name": "B", "status": "error", "error": "こわれた"}]
        text = runner.format_digest(
            {"pending": []}, results,
            datetime.datetime(2026, 10, 7, 19, 0, tzinfo=JST))
        for expect in ("A", "B", "こわれた"):
            self.assertIn(expect, text)


class TestState(unittest.TestCase):
    def test_namespaces_are_isolated_per_watch(self):
        data = state.load("/nonexistent/none.json")
        state.for_watch(data, "w1")["active"] = ["a"]
        state.for_watch(data, "w2")["active"] = ["b"]
        self.assertEqual(data["watches"]["w1"]["active"], ["a"])
        self.assertEqual(data["watches"]["w2"]["active"], ["b"])

    def test_corrupt_file_does_not_crash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            f.write("{ broken")
            path = f.name
        self.assertEqual(state.load(path)["watches"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
