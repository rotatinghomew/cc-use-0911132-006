"""按入组时间重建人物旅程并汇总分层漏斗。

漏斗层级（每层都显式记录进入条件、人数与未达观察期人数）：

1. ``enrolled`` —— 首次到访（``first_visit``）即入组，同一自然人只入组一次，
   入组渠道取首次到访事件的渠道，入组市场取首次到访市场；
2. ``consulted`` —— 入组后在任意市场发生咨询；
3. ``purchased_gross`` —— 入组后 :math:`H` 天内任意市场成交（毛口径，
   跨市场成交计入人物终点，但保留市场轨迹可追溯）；
4. ``purchased_net`` —— 毛成交中未在窗口内被全额退款冲销的部分（净口径）。

未等满 :math:`H` 天的人物在每一层都进入 ``not_mature`` 计数，绝不简单判失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from twin_market_flow.funnel_cohorts.identity import IdentityResolution
from twin_market_flow.funnel_cohorts.statistics import (
    arrival_distribution,
    kaplan_meier,
    wilson_interval,
)
from twin_market_flow.funnel_cohorts.timeline import (
    add_days,
    day_offset,
    iso,
    monday_label,
    parse_ts,
    round6,
)

STAGES = (
    {
        "stage": "enrolled",
        "entry_rule": "person-level first visit (first_visit); one person enters exactly once even when visiting both markets repeatedly",
    },
    {
        "stage": "consulted",
        "entry_rule": "at least one consult event at or after enrollment, any market",
    },
    {
        "stage": "purchased_gross",
        "entry_rule": "at least one purchase within H days after enrollment in any market; cross-market purchase counts for the person and keeps market trace",
    },
    {
        "stage": "purchased_net",
        "entry_rule": "gross purchase not fully refunded within the H-day window; refunds only affect their own order_id",
    },
)


@dataclass(slots=True)
class PersonJourney:
    person_id: str
    devices: list[str]
    channel: str
    entry_market: str
    enrollment_at: datetime
    enrollment_week: str
    markets: set[str] = field(default_factory=set)
    consult_day: int | None = None
    gross_day: int | None = None
    net_day: int | None = None
    gross_orders: list[dict[str, Any]] = field(default_factory=list)
    net_orders: list[dict[str, Any]] = field(default_factory=list)
    refunded_orders: list[dict[str, Any]] = field(default_factory=list)
    post_window_refunded_orders: list[dict[str, Any]] = field(default_factory=list)
    censor_day: int = 0
    mature: bool = False

    def as_trace(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "devices": self.devices,
            "channel": self.channel,
            "entry_market": self.entry_market,
            "enrollment_at": iso(self.enrollment_at),
            "enrollment_week": self.enrollment_week,
            "markets": sorted(self.markets),
            "consult_day": self.consult_day,
            "gross_convert_day": self.gross_day,
            "net_convert_day": self.net_day,
            "censor_day": self.censor_day,
            "mature": self.mature,
            "gross_orders": self.gross_orders,
            "net_orders": self.net_orders,
            "refunded_orders": self.refunded_orders,
            "post_window_refunded_orders": self.post_window_refunded_orders,
        }


def build_person_journeys(
    events: list[dict[str, Any]],
    refunds: list[dict[str, Any]],
    identity: IdentityResolution,
    cutoff: datetime,
    horizon_days: int,
) -> list[PersonJourney]:
    """把设备事件折叠为人物旅程，只采用截止时点可知的事实。"""
    known_events = [
        event for event in events if parse_ts(event["available_at"]) <= cutoff
    ]
    known_refunds = {
        str(item["order_id"]): item
        for item in refunds
        if parse_ts(item["available_at"]) <= cutoff
    }

    by_person: dict[str, list[dict[str, Any]]] = {}
    for event in known_events:
        person_id = identity.person_of(str(event["device_id"]))
        by_person.setdefault(person_id, []).append(event)

    journeys: list[PersonJourney] = []
    for person_id in sorted(by_person, key=lambda value: (0, value) if value.startswith("person-") else (1, value)):
        person_events = sorted(
            by_person[person_id],
            key=lambda item: (parse_ts(item["occurred_at"]), str(item["event_id"])),
        )
        visits = [event for event in person_events if event["kind"] == "visit"]
        if not visits:
            # 只有咨询/成交而无到访，不满足 first_visit 入组条件，不进入漏斗。
            continue
        first_visit = visits[0]
        enrollment = parse_ts(first_visit["occurred_at"])
        horizon_end = add_days(enrollment, horizon_days)
        censor_day = max(0, min(horizon_days, day_offset(cutoff, enrollment)))
        mature = horizon_end <= cutoff

        journey = PersonJourney(
            person_id=person_id,
            devices=sorted(
                {str(event["device_id"]) for event in person_events},
                key=lambda value: int(value.split("-")[-1]) if value.rsplit("-", 1)[-1].isdigit() else value,
            ),
            channel=str(first_visit.get("channel") or "unknown"),
            entry_market=str(first_visit.get("market") or "unknown"),
            enrollment_at=enrollment,
            enrollment_week=monday_label(enrollment),
            censor_day=censor_day,
            mature=mature,
        )
        for event in person_events:
            journey.markets.add(str(event.get("market")))

        consults = [
            day_offset(parse_ts(event["occurred_at"]), enrollment)
            for event in person_events
            if event["kind"] == "consult"
        ]
        consults = [day for day in consults if 0 <= day <= horizon_days]
        if consults:
            journey.consult_day = min(consults)

        for event in person_events:
            if event["kind"] != "purchase":
                continue
            order_id = event.get("order_id")
            day = day_offset(parse_ts(event["occurred_at"]), enrollment)
            if day < 0 or day > horizon_days:
                continue
            market = str(event.get("market") or "unknown")
            refund = known_refunds.get(str(order_id)) if order_id else None
            full_refund = bool(refund and refund.get("full_refund"))
            refund_in_window = False
            if refund is not None:
                refund_day = day_offset(parse_ts(refund["effective_at"]), enrollment)
                refund_in_window = 0 <= refund_day <= horizon_days
            order_trace = {
                "order_id": order_id,
                "market": market,
                "cross_market": market != journey.entry_market,
                "convert_day": day,
                "refunded_full_in_window": bool(full_refund and refund_in_window),
            }
            journey.gross_orders.append(order_trace)
            if full_refund and refund_in_window:
                journey.refunded_orders.append(
                    {
                        "order_id": order_id,
                        "market": market,
                        "convert_day": day,
                        "refund_effective_at": refund["effective_at"],
                        "refund_known_at": refund["available_at"],
                    }
                )
                # 窗口内全额退款冲销净终点，该订单不进入 net_orders。
                continue
            if full_refund:
                # 窗口外才生效的全额退款可追溯，但不能追溯改写 30 天净终点：
                # 订单仍计入 purchased_net，同时登记在案。
                journey.post_window_refunded_orders.append(
                    {
                        "order_id": order_id,
                        "market": market,
                        "convert_day": day,
                        "refund_effective_at": refund["effective_at"],
                        "refund_known_at": refund["available_at"],
                        "refund_day_from_enrollment": day_offset(
                            parse_ts(refund["effective_at"]), enrollment
                        ),
                    }
                )
            # 无退款、部分退款或窗口外全额退款都保留净转化。
            journey.net_orders.append(order_trace)

        if journey.gross_orders:
            journey.gross_day = min(item["convert_day"] for item in journey.gross_orders)
        if journey.net_orders:
            journey.net_day = min(item["convert_day"] for item in journey.net_orders)
        journeys.append(journey)

    journeys.sort(key=lambda item: item.enrollment_at)
    return journeys


def _stage_records(journeys: list[PersonJourney], stage: str) -> list[dict[str, Any]]:
    records = []
    for journey in journeys:
        if stage == "enrolled":
            event_day = 0
        elif stage == "consulted":
            event_day = journey.consult_day
        elif stage == "purchased_gross":
            event_day = journey.gross_day
        elif stage == "purchased_net":
            event_day = journey.net_day
        else:  # pragma: no cover - 防御性分支
            raise ValueError(f"unknown stage: {stage}")
        records.append({"event_day": event_day, "censor_day": journey.censor_day})
    return records


def _funnel_row(
    stage_meta: dict[str, str],
    all_journeys: list[PersonJourney],
    entered_journeys: list[PersonJourney],
    stage: str,
    horizon_days: int,
    prior_entered: int,
) -> dict[str, Any]:
    """构造单层漏斗。

    乘积极限的风险集始终是本分段全部入组人物（含未到达本层者与未成熟者），
    未到达者在窗口结束/观察截止处右删失；``entered_journeys`` 只用于
    本层进入人数与相邻层条件占比，不能拿来当 KM 风险集。
    """
    entered = len(entered_journeys)
    not_mature = sum(1 for item in all_journeys if not item.mature)
    mature_count = len(all_journeys) - not_mature
    km = kaplan_meier(_stage_records(all_journeys, stage), horizon_days)
    # 朴素占比：未成熟人物被当成未转化，仅用于演示月报口径偏差，不参与排名。
    all_records = _stage_records(all_journeys, stage)
    observed_events = sum(
        1 for record in all_records if record["event_day"] is not None
    )
    # 成熟风险集内的事件数：未成熟人物即使已提前转化也不计入成熟分子。
    mature_records = [
        record
        for record, journey in zip(all_records, all_journeys)
        if journey.mature
    ]
    mature_observed_events = sum(
        1 for record in mature_records if record["event_day"] is not None
    )
    naive = wilson_interval(observed_events, len(all_journeys))
    conditional = (
        wilson_interval(observed_events, prior_entered) if prior_entered > 0 else
        {"point": None, "low": None, "high": None}
    )
    return {
        "stage": stage,
        "entry_rule": stage_meta["entry_rule"],
        "entered_persons": entered,
        "not_mature_persons": not_mature,
        "mature_at_risk_persons": mature_count,
        "observed_events": observed_events,
        "mature_observed_events": mature_observed_events,
        "naive_proportion_including_immature": naive,
        "conditional_proportion_vs_prior_stage": conditional,
        "km_horizon_conversion": {
            "point": km["p_horizon"],
            "ci_low": km["ci_low"],
            "ci_high": km["ci_high"],
        },
        "km_detail": {
            "at_risk": km["at_risk"],
            "events": km["events"],
            "censored": km["censored"],
            "rmst_days": km["rmst_days"],
            "median_convert_day": km["median_convert_day"],
            "survival_curve": km["survival_curve"],
        },
    }


def build_funnel(
    journeys: list[PersonJourney], horizon_days: int
) -> list[dict[str, Any]]:
    """分层漏斗；每层 KM 风险集都是该分段的全部入组人物。"""
    rows = []
    prior_entered = 0
    for index, stage_meta in enumerate(STAGES):
        stage = stage_meta["stage"]
        if index == 0:
            eligible = list(journeys)
        elif stage == "consulted":
            eligible = [item for item in journeys if item.consult_day is not None]
        elif stage == "purchased_gross":
            eligible = [item for item in journeys if item.gross_day is not None]
        elif stage == "purchased_net":
            eligible = [item for item in journeys if item.net_day is not None]
        rows.append(
            _funnel_row(
                stage_meta, journeys, eligible, stage, horizon_days, prior_entered
            )
        )
        prior_entered = len(eligible)
    return rows


def build_time_to_convert(
    journeys: list[PersonJourney], horizon_days: int
) -> dict[str, Any]:
    """三个转化终点的到达时间分布与 KM 不确定性。"""
    endpoints = (
        ("consult", "consult_day"),
        ("purchase_gross", "gross_day"),
        ("purchase_net", "net_day"),
    )
    result: dict[str, Any] = {"horizon_days": horizon_days, "endpoints": {}}
    for name, attribute in endpoints:
        records = [
            {"event_day": getattr(item, attribute), "censor_day": item.censor_day}
            for item in journeys
        ]
        km = kaplan_meier(records, horizon_days)
        distribution = arrival_distribution(
            [getattr(item, attribute) for item in journeys if getattr(item, attribute) is not None],
            horizon_days,
        )
        result["endpoints"][name] = {
            "arrival_distribution": distribution,
            "km_horizon_conversion": {
                "point": km["p_horizon"],
                "ci_low": km["ci_low"],
                "ci_high": km["ci_high"],
            },
            "rmst_days": km["rmst_days"],
            "median_convert_day": km["median_convert_day"],
            "survival_curve": km["survival_curve"],
            "at_risk": km["at_risk"],
            "censored": km["censored"],
        }
    return result


def segment_journeys(
    journeys: list[PersonJourney], dimension: str
) -> dict[str, list[PersonJourney]]:
    grouped: dict[str, list[PersonJourney]] = {}
    for journey in journeys:
        if dimension == "overall":
            key = "all"
        elif dimension == "channel":
            key = journey.channel
        elif dimension == "enrollment_week":
            key = journey.enrollment_week
        elif dimension == "week_channel":
            key = f"{journey.enrollment_week}|{journey.channel}"
        else:  # pragma: no cover
            raise ValueError(f"unknown dimension: {dimension}")
        grouped.setdefault(key, []).append(journey)
    return dict(sorted(grouped.items()))
