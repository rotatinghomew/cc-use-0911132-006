"""漏斗队列分析的业务反例与统计正确性测试。

覆盖：

* 晚到成交 + 未成熟随访不得把渠道“变差”伪造出来（KM 右删失 vs 朴素口径）；
* 误合并撤回的身份重述：旧版运行不可变、新版可重新解释历史；
* 同一人反复进入两个市场只算一位新客；同终端 split 后归属不同人；
* 窗口内/窗口外退款对净终点的不同影响；跨市场成交保留轨迹且仍计人物终点；
* 无支撑渠道不得排名；统一观察长度、到达时间分布与不确定性；
* 重放幂等、上游 visitor_links / sessionize 运行关联、CLI 契约。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from twin_market_flow.funnel_cohorts.analysis import run_analysis
from twin_market_flow.funnel_cohorts.cohorts import build_person_journeys
from twin_market_flow.funnel_cohorts.identity import (
    distinct_identity_versions,
    resolve_identity,
)
from twin_market_flow.funnel_cohorts.statistics import (
    arrival_distribution,
    difference_interval,
    kaplan_meier,
    wilson_interval,
)
from twin_market_flow.funnel_cohorts.storage import FunnelCohortStore
from twin_market_flow.funnel_cohorts.timeline import parse_ts
from twin_market_flow.service import ApplicationService

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures/analysis/input.json"
HORIZON = 30


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def write_input(directory: Path, payload: dict) -> Path:
    path = directory / "input.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def event(
    event_id: str,
    device: str,
    kind: str,
    occurred: str,
    channel: str = "event",
    market: str = "south",
    order_id: str | None = None,
    available: str | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "device_id": device,
        "market": market,
        "kind": kind,
        "occurred_at": occurred,
        "available_at": available or occurred,
        "channel": channel,
        "order_id": order_id,
    }


def visit(event_id: str, device: str, occurred: str, channel: str, market: str = "south") -> dict:
    return event(event_id, device, "visit", occurred, channel, market)


def consult(event_id: str, device: str, occurred: str, channel: str, market: str = "north") -> dict:
    return event(event_id, device, "consult", occurred, channel, market)


def purchase(event_id: str, device: str, occurred: str, channel: str, order_id: str, market: str = "south", available: str | None = None) -> dict:
    return event(event_id, device, "purchase", occurred, channel, market, order_id, available)


def policy(cutoffs: list[str], horizon: int = HORIZON) -> dict:
    return {"entry": "first_visit", "horizon_days": horizon, "historical_cutoffs": cutoffs}


class StatisticsTests(unittest.TestCase):
    def test_wilson_interval_covers_point(self):
        interval = wilson_interval(3, 10)
        self.assertLessEqual(interval["low"], interval["point"])
        self.assertLessEqual(interval["point"], interval["high"])
        self.assertIsNone(wilson_interval(0, 0)["point"])

    def test_km_fixed_horizon_with_right_censoring(self):
        # 10 人：4 人第 6 天转化，6 人只观察到第 10 天就截止（未成熟）。
        records = [{"event_day": 6, "censor_day": 30}] * 4 + [
            {"event_day": None, "censor_day": 10}
        ] * 6
        result = kaplan_meier(records, HORIZON)
        # 第 6 天风险集 10、事件 4，之后 6 人删失；S(30)=0.6，p30=0.4。
        self.assertAlmostEqual(result["p_horizon"], 0.4, places=6)
        self.assertEqual(result["events"], 4)
        self.assertEqual(result["censored"], 6)
        self.assertGreaterEqual(result["ci_low"], 0.0)
        self.assertLessEqual(result["ci_high"], 1.0)
        self.assertLess(result["ci_low"], result["p_horizon"] < result["ci_high"])
        # 未成熟人物绝不计入事件，也不被当失败：朴素 4/10 与 KM 0.4 在此相同，
        # 但当事件晚于部分删失点时二者会分离（见业务测试）。

    def test_km_mature_nonconverters_count_as_survivors(self):
        records = [{"event_day": 24, "censor_day": 30}] * 12 + [
            {"event_day": None, "censor_day": 30}
        ] * 13
        result = kaplan_meier(records, HORIZON)
        self.assertAlmostEqual(result["p_horizon"], 0.48, places=6)
        self.assertIsNone(result["median_convert_day"])
        # RMST = 24 天全存活 + 末 6 天以 S=0.52 存活 = 24 + 3.12。
        self.assertAlmostEqual(result["rmst_days"], 27.12, places=6)

    def test_km_no_events_and_all_events_bounds(self):
        empty = kaplan_meier([{"event_day": None, "censor_day": 30}] * 10, HORIZON)
        self.assertEqual(empty["p_horizon"], 0.0)
        self.assertEqual(empty["ci_low"], 0.0)
        self.assertGreater(empty["ci_high"], 0.0)
        full = kaplan_meier([{"event_day": 6, "censor_day": 30}] * 5, HORIZON)
        self.assertEqual(full["p_horizon"], 1.0)
        self.assertEqual(full["ci_low"], 1.0)
        self.assertEqual(full["ci_high"], 1.0)
        self.assertEqual(full["median_convert_day"], 6)

    def test_difference_interval_and_distribution(self):
        diff = difference_interval(20, 25, 12, 25)
        self.assertAlmostEqual(diff["difference"], 0.32, places=6)
        self.assertGreater(diff["ci_high"], diff["difference"])
        distribution = arrival_distribution([6, 6, 24, 24], HORIZON)
        self.assertEqual(distribution["converters"], 4)
        self.assertEqual(distribution["p50_day"], 15.0)
        self.assertEqual(distribution["min_day"], 6)


class IdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.changes = load_fixture()["identity_changes"]

    def test_merge_only_visible_after_available_at(self):
        early = resolve_identity(self.changes, "2026-08-10T00:00:00+00:00", "iv0")
        self.assertEqual(early.effective_changes, [])
        self.assertEqual(early.person_of("device-1"), "device:device-1")
        merged = resolve_identity(self.changes, "2026-09-15T00:00:00+00:00", "iv1")
        self.assertEqual(merged.effective_changes, ["link-1"])
        self.assertEqual(merged.active_merges, ["link-1"])
        self.assertEqual(merged.person_of("device-1"), "person-1")
        self.assertEqual(merged.person_of("device-2"), "person-1")

    def test_split_supersedes_merge_and_reassigns_device(self):
        split = resolve_identity(self.changes, "2026-10-30T00:00:00+00:00", "iv2")
        # 两个事实在该时点都可知；merge 已撤回，只有 split 仍生效。
        self.assertEqual(split.effective_changes, ["link-1", "unlink-1"])
        self.assertEqual(split.active_merges, [])
        self.assertEqual(split.person_of("device-1"), "person-1")
        self.assertEqual(split.person_of("device-2"), "person-2")

    def test_distinct_versions_dedup_identical_cutoffs(self):
        versions = distinct_identity_versions(
            self.changes,
            ["2026-08-10T00:00:00+00:00", "2026-09-15T00:00:00+00:00", "2026-10-30T00:00:00+00:00"],
        )
        ids = [item[0] for item in versions]
        self.assertEqual(ids, ["iv1", "iv2", "iv3"])
        # 两个口径相同的时点解析到同一版本。
        repeat = distinct_identity_versions(
            self.changes, ["2026-09-15T00:00:00+00:00", "2026-09-16T00:00:00+00:00"]
        )
        self.assertEqual(repeat[0][0], repeat[1][0])


class CohortJourneyTests(unittest.TestCase):
    def _payload(self, tmp: Path) -> dict:
        payload = {
            "schema_version": 1,
            "as_of": "2026-10-30T00:00:00+00:00",
            "source_versions": {"operations": "t1"},
            "events": [
                visit("v1", "device-a", "2026-09-01T00:00:00+00:00", "natural", "south"),
                # 同一人在另一市场反复到访，不得产生第二位新客。
                visit("v2", "device-a", "2026-09-03T00:00:00+00:00", "natural", "north"),
                consult("c1", "device-a", "2026-09-05T00:00:00+00:00", "natural"),
                purchase("p1", "device-a", "2026-09-20T00:00:00+00:00", "natural", "order-a", market="north"),
                visit("v3", "device-b", "2026-09-02T00:00:00+00:00", "event", "south"),
                consult("c2", "device-b", "2026-09-04T00:00:00+00:00", "event"),
                purchase("p2", "device-b", "2026-09-08T00:00:00+00:00", "event", "order-b"),
            ],
            "identity_changes": [],
            "refunds": [],
            "cohort_policy": policy(["2026-10-30T00:00:00+00:00"]),
            "baseline_case_id": "case-x",
        }
        return payload

    def test_repeated_cross_market_visits_are_one_person(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            payload = self._payload(tmp)
            path = write_input(tmp, payload)
            output = run_analysis(path, tmp / "db.sqlite3")
            self.assertEqual(len(output["person_journeys"]), 2)
            journey_a = next(j for j in output["person_journeys"] if "device-a" in j["devices"])
            self.assertEqual(journey_a["markets"], ["north", "south"])
            self.assertEqual(journey_a["gross_orders"][0]["cross_market"], True)
            # 跨市场成交仍计人物终点，市场轨迹保留可追溯。
            self.assertEqual(journey_a["gross_convert_day"], 19)
            overall = next(s for s in output["funnels"] if s["dimension"] == "overall")
            enrolled = next(r for r in overall["funnel"] if r["stage"] == "enrolled")
            self.assertEqual(enrolled["entered_persons"], 2)

    def test_in_window_refund_removes_net_but_keeps_gross(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            payload = self._payload(tmp)
            payload["refunds"] = [
                {
                    "order_id": "order-b",
                    "effective_at": "2026-09-10T00:00:00+00:00",
                    "available_at": "2026-09-11T00:00:00+00:00",
                    "full_refund": True,
                }
            ]
            path = write_input(tmp, payload)
            output = run_analysis(path, tmp / "db.sqlite3")
            journey_b = next(j for j in output["person_journeys"] if "device-b" in j["devices"])
            self.assertEqual(journey_b["gross_convert_day"], 6)
            self.assertIsNone(journey_b["net_convert_day"])
            overall = next(s for s in output["funnels"] if s["dimension"] == "overall")
            gross = next(r for r in overall["funnel"] if r["stage"] == "purchased_gross")
            net = next(r for r in overall["funnel"] if r["stage"] == "purchased_net")
            self.assertEqual(gross["observed_events"], 2)
            self.assertEqual(net["observed_events"], 1)
            self.assertEqual(output["endpoint_traces"]["refunded_orders"][0]["order_id"], "order-b")

    def test_post_window_refund_is_traced_but_keeps_net(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            payload = self._payload(tmp)
            # device-b 2026-09-02 入组、第 6 天成交；退款在入组第 35 天生效，
            # 落在 30 天窗口之外，只能登记追溯，不能改写净终点。
            payload["refunds"] = [
                {
                    "order_id": "order-b",
                    "effective_at": "2026-10-07T00:00:00+00:00",
                    "available_at": "2026-10-08T00:00:00+00:00",
                    "full_refund": True,
                }
            ]
            path = write_input(tmp, payload)
            output = run_analysis(path, tmp / "db.sqlite3")
            journey_b = next(j for j in output["person_journeys"] if "device-b" in j["devices"])
            self.assertEqual(journey_b["net_convert_day"], 6)
            self.assertEqual(len(journey_b["post_window_refunded_orders"]), 1)
            self.assertEqual(len(output["endpoint_traces"]["refunded_orders"]), 1)
            net = next(
                r
                for s in output["funnels"]
                if s["dimension"] == "overall"
                for r in s["funnel"]
                if r["stage"] == "purchased_net"
            )
            self.assertEqual(net["observed_events"], 2)

    def test_unknown_refund_before_available_at_is_not_used(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            payload = self._payload(tmp)
            payload["as_of"] = "2026-09-12T00:00:00+00:00"
            payload["cohort_policy"] = policy(["2026-09-12T00:00:00+00:00"])
            payload["refunds"] = [
                {
                    "order_id": "order-b",
                    "effective_at": "2026-09-10T00:00:00+00:00",
                    "available_at": "2026-10-01T00:00:00+00:00",
                    "full_refund": True,
                }
            ]
            path = write_input(tmp, payload)
            output = run_analysis(path, tmp / "db.sqlite3")
            journey_b = next(j for j in output["person_journeys"] if "device-b" in j["devices"])
            self.assertEqual(journey_b["net_convert_day"], 6)
            self.assertEqual(output["endpoint_traces"]["refunded_orders"], [])


class LateArrivalBiasTests(unittest.TestCase):
    """晚到成交反例：观察期差异不得伪造成渠道变差。"""

    def _payload(self, tmp: Path) -> dict:
        from twin_market_flow.funnel_cohorts.timeline import add_days, iso
        events = []
        origin = parse_ts("2026-09-01T00:00:00+00:00")
        # 10 个 event 渠道：入组后第 6 天成交；10 个 natural 渠道：第 24 天成交。
        for index in range(10):
            device = f"device-e{index}"
            enroll = iso(add_days(origin, index))
            events.append(visit(f"ve{index}", device, enroll, "event"))
            events.append(
                purchase(
                    f"pe{index}", device, iso(add_days(origin, index + 6)),
                    "event", f"order-e{index}",
                    available=iso(add_days(origin, index + 7)),
                )
            )
        for index in range(10):
            device = f"device-n{index}"
            enroll = iso(add_days(origin, index))
            events.append(visit(f"vn{index}", device, enroll, "natural"))
            events.append(
                purchase(
                    f"pn{index}", device, iso(add_days(origin, index + 24)),
                    "natural", f"order-n{index}",
                    available=iso(add_days(origin, index + 25)),
                )
            )
        return {
            "schema_version": 1,
            "as_of": "2026-10-30T00:00:00+00:00",
            "source_versions": {"operations": "t1"},
            "events": events,
            "identity_changes": [],
            "refunds": [],
            "cohort_policy": policy([
                "2026-09-15T00:00:00+00:00",
                "2026-10-30T00:00:00+00:00",
            ]),
            "baseline_case_id": "case-x",
        }

    def test_immature_cohort_is_censored_not_failed(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            path = write_input(tmp, self._payload(tmp))
            output = run_analysis(path, tmp / "db.sqlite3")
            early = next(s for s in output["cutoff_summaries"] if s["cutoff_at"].startswith("2026-09-15"))
            late = next(s for s in output["cutoff_summaries"] if s["cutoff_at"].startswith("2026-10-30"))
            natural_early = next(
                (c for c in early["channels"] if c["channel"] == "natural"), None
            )
            event_early = next(c for c in early["channels"] if c["channel"] == "event")
            # 9 月 15 日时自然渠道的到访事实还没到 available_at（分段缺失），
            # 或已可知人物全部未等满 30 天、成熟风险集为 0：两种情况下都不允许
            # 把“看不到转化”当成失败，更不能据此判定渠道变差或参与排名。
            if natural_early is None:
                self.assertNotIn(
                    "natural",
                    {c["channel"] for c in output["comparability"]["channel_cells"] if c["ranked"]},
                )
            else:
                # 自然渠道没有成熟风险集：即便 KM 给出 0.0 的现时点估计，
                # 其上置信界明显大于 0 且不允许参与排名——不能当成“质量为零”。
                self.assertEqual(natural_early["mature_at_risk_persons"], 0)
                self.assertFalse(natural_early["ranked"])
                self.assertGreater(natural_early["gross_km_ci"][1], 0.0)
            # event 渠道成交快（第 6 天）：已有可知人物与已观察到的转化，
            # 但在 9 月 15 日同样没有等满 30 天的人物，不能对其 30 天概率下结论。
            self.assertGreaterEqual(event_early["entered_persons"], 1)
            self.assertEqual(event_early["mature_at_risk_persons"], 0)
            self.assertFalse(event_early["ranked"])
            natural_late = next(c for c in late["channels"] if c["channel"] == "natural")
            # 等满窗口、事实到齐后自然渠道全部转化：“变差”是观察期伪影。
            self.assertEqual(natural_late["entered_persons"], 10)
            self.assertEqual(natural_late["gross_km_conversion_30d"], 1.0)
            self.assertEqual(natural_late["not_mature_persons"], 0)
            proof = next(p for p in output["observation_window_proof"] if p["channel"] == "natural")
            self.assertEqual(proof["series"][0]["mature_at_risk_persons"], 0)
            self.assertEqual(proof["series"][1]["mature_km_conversion_30d"], 1.0)

    def test_unsupported_channel_is_not_ranked(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            payload = self._payload(tmp)
            # 新增一个只有 2 人的小众渠道，低于 MIN_MATURE_SUPPORT=5。
            payload["events"] += [
                visit("vx1", "device-x1", "2026-09-10T00:00:00+00:00", "referral"),
                purchase("px1", "device-x1", "2026-09-16T00:00:00+00:00", "referral", "order-x1"),
                visit("vx2", "device-x2", "2026-09-11T00:00:00+00:00", "referral"),
            ]
            path = write_input(tmp, payload)
            output = run_analysis(path, tmp / "db.sqlite3")
            referral = next(c for c in output["comparability"]["channel_cells"] if c["channel"] == "referral")
            self.assertLess(referral["mature_at_risk_persons"], 5)
            self.assertFalse(referral["ranked"])
            self.assertFalse(referral["supported_for_ranking"])
            ranked = {c["channel"] for c in output["comparability"]["channel_cells"] if c["ranked"]}
            self.assertNotIn("referral", ranked)


class IdentityRestatementTests(unittest.TestCase):
    """误合并撤回：身份重述不偷偷改掉旧版结果。"""

    def _payload(self, tmp: Path) -> dict:
        return {
            "schema_version": 1,
            "as_of": "2026-10-30T00:00:00+00:00",
            "source_versions": {"operations": "t1"},
            "events": [
                visit("v1", "device-1", "2026-08-02T00:00:00+00:00", "event"),
                purchase("p1", "device-1", "2026-08-08T00:00:00+00:00", "event", "order-1"),
                visit("v2", "device-2", "2026-08-03T00:00:00+00:00", "natural"),
                purchase("p2", "device-2", "2026-08-27T00:00:00+00:00", "natural", "order-2"),
            ],
            "identity_changes": [
                {
                    "change_id": "link-1",
                    "device_ids": ["device-1", "device-2"],
                    "person_id": "person-1",
                    "kind": "merge",
                    "effective_at": "2026-08-01T00:00:00+00:00",
                    "available_at": "2026-08-21T00:00:00+00:00",
                },
                {
                    "change_id": "unlink-1",
                    "device_ids": ["device-2"],
                    "person_id": "person-2",
                    "kind": "split",
                    "supersedes": "link-1",
                    "effective_at": "2026-08-01T00:00:00+00:00",
                    "available_at": "2026-10-10T00:00:00+00:00",
                },
            ],
            "refunds": [],
            "cohort_policy": policy([
                "2026-09-15T00:00:00+00:00",
                "2026-10-30T00:00:00+00:00",
            ]),
            "baseline_case_id": "case-x",
        }

    def test_merge_then_split_restatement_keeps_history_immutable(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            path = write_input(tmp, self._payload(tmp))
            first = run_analysis(path, tmp / "db.sqlite3")
            early_run, late_run = first["run_catalog"]
            self.assertNotEqual(early_run["cohort_run_id"], late_run["cohort_run_id"])
            # 旧口径：两台设备误合并为 person-1，只有 1 位人物。
            store = FunnelCohortStore(tmp / "db.sqlite3")
            stored_early = store.get_run(early_run["cohort_run_id"])
            early_result = stored_early["result"]
            self.assertEqual(early_result["identity"]["distinct_persons"], 1)
            self.assertEqual(early_result["identity"]["effective_changes"], ["link-1"])
            # 新口径：撤回合并后为 2 位人物，历史被重新解释但旧记录原样保留。
            self.assertEqual(late_run["identity_version"], "iv2")
            restatement = first["identity_effects"]["restatements"][0]
            self.assertTrue(restatement["historical_run_immutable"])
            self.assertEqual(restatement["prior_distinct_persons"], 1)
            self.assertEqual(restatement["restated_distinct_persons"], 2)
            self.assertIn("person-2", restatement["persons_added_by_restatement"])
            # 再次运行：同一输入返回同一批 cohort_run_id，且旧指纹不变。
            replay = run_analysis(path, tmp / "db.sqlite3")
            self.assertEqual(replay["cohort_run_id"], first["cohort_run_id"])
            self.assertEqual(
                [item["cohort_run_id"] for item in replay["run_catalog"]],
                [item["cohort_run_id"] for item in first["run_catalog"]],
            )
            stored_early_again = store.get_run(early_run["cohort_run_id"])
            self.assertEqual(
                stored_early_again["result_fingerprint"], stored_early["result_fingerprint"]
            )
            self.assertTrue(
                all(item["immutable"] for item in replay["identity_effects"]["stored_run_fingerprints"])
            )

    def test_split_person_keeps_own_channel_and_journey(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            path = write_input(tmp, self._payload(tmp))
            output = run_analysis(path, tmp / "db.sqlite3")
            persons = {j["person_id"]: j for j in output["person_journeys"]}
            self.assertEqual(persons["person-1"]["channel"], "event")
            self.assertEqual(persons["person-2"]["channel"], "natural")
            self.assertEqual(persons["person-2"]["devices"], ["device-2"])


class FixtureContractTests(unittest.TestCase):
    def test_fixture_end_to_end_counts_and_bias_proof(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            db = tmp / "analysis.sqlite3"
            # 先跑既有产品，验证同库关联。
            ApplicationService(db).execute(
                {"case_id": "twin_market_flow-demo"}, request_key="baseline"
            )
            output = run_analysis(FIXTURE, db, tmp / "funnel.json")
            required = [
                "cohort_run_id", "funnels", "time_to_convert", "identity_effects",
                "comparability", "sources",
            ]
            for key in required:
                self.assertIn(key, output)
            self.assertEqual(len(output["run_catalog"]), 2)
            # 最新截止时点 50 台设备 = 50 位人物（split 已生效）。
            self.assertEqual(len(output["person_journeys"]), 50)
            event = next(c for c in output["comparability"]["channel_cells"] if c["channel"] == "event")
            natural = next(c for c in output["comparability"]["channel_cells"] if c["channel"] == "natural")
            self.assertEqual(event["gross_conversion_30d"]["point"], 1.0)
            self.assertEqual(natural["gross_conversion_30d"]["point"], 0.48)
            # 早截止时点的成熟估计不支持“自然渠道变差”：0.40（0915 成熟）→ 0.48（满窗）。
            early = next(s for s in output["cutoff_summaries"] if s["cutoff_at"].startswith("2026-09-15"))
            early_natural = next(c for c in early["channels"] if c["channel"] == "natural")
            self.assertEqual(early_natural["gross_km_conversion_30d"], 0.4)
            self.assertAlmostEqual(early_natural["gross_naive_proportion"], 0.181818, places=5)
            # 上游运行关联。
            stages = {link["stage"] for link in output["sources"]["upstream_runs"]}
            self.assertIn("visitor_links", stages)
            self.assertIn("journey_sessionize", stages)
            # 退款可追溯且未改写净 30 天终点。
            self.assertEqual(output["endpoint_traces"]["refunded_orders"][0]["order_id"], "order-2")
            self.assertEqual(natural["net_conversion_30d"]["point"], 0.48)
            # 时间分布：event 第 6 天、natural 第 24 天两个模态。
            gross = output["time_to_convert"]["endpoints"]["purchase_gross"]
            days = {item["day"] for item in gross["arrival_distribution"]["histogram"]}
            self.assertEqual(days, {6, 24})

    def test_cli_module_contract_and_replay(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            db = tmp / "analysis.sqlite3"
            first_out = tmp / "first.json"
            replay_out = tmp / "replay.json"
            base = [
                sys.executable, "-m", "twin_market_flow.api.command",
                str(REPO / "fixtures/demo.json"), "--database", str(db),
            ]
            subprocess.run(base, check=True, cwd=REPO, capture_output=True, text=True)
            cmd = [
                sys.executable, "-m", "twin_market_flow.funnel_cohorts",
                "--input", str(FIXTURE), "--database", str(db), "--output",
            ]
            run_one = subprocess.run(cmd + [str(first_out)], check=True, cwd=REPO, capture_output=True, text=True)
            self.assertIn("cohort_run_id", run_one.stdout)
            subprocess.run(cmd + [str(replay_out)], check=True, cwd=REPO, capture_output=True, text=True)
            first = json.loads(first_out.read_text())
            replay = json.loads(replay_out.read_text())
            self.assertEqual(first["cohort_run_id"], replay["cohort_run_id"])
            self.assertEqual(first, replay)

    def test_rejects_unknown_policy_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            payload = load_fixture()
            payload["cohort_policy"]["entry"] = "last_visit"
            path = write_input(tmp, payload)
            with self.assertRaises(Exception):
                run_analysis(path, tmp / "db.sqlite3")
            bad = load_fixture()
            del bad["refunds"]
            bad_path = write_input(tmp, bad)
            with self.assertRaises(Exception):
                run_analysis(bad_path, tmp / "db2.sqlite3")


if __name__ == "__main__":
    unittest.main()
