"""漏斗队列分析编排。

处理顺序：

1. 校验并载入旅程 JSON，计算输入指纹；
2. 对 ``cohort_policy.historical_cutoffs`` 中不晚于 ``as_of`` 的每个截止时点，
   按当时可知的身份事实解析身份版本；
3. 折叠人物旅程（同一自然人只入组一次），在统一 :math:`H` 天观察长度下
   估计分层漏斗、到达时间分布与不确定性，未成熟人物右删失；
4. 汇总身份重述影响、跨入组周/渠道可比性，并把每个运行写入 SQLite 台账，
   关联同库的 visitor_links 与 journey_sessionize 运行。

本模块不做随机实验、不改动 ``attribution/incrementality.py`` 的任何结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from twin_market_flow.funnel_cohorts.cohorts import (
    STAGES,
    PersonJourney,
    build_funnel,
    build_person_journeys,
    build_time_to_convert,
    segment_journeys,
)
from twin_market_flow.funnel_cohorts.identity import (
    IdentityResolution,
    distinct_identity_versions,
)
from twin_market_flow.funnel_cohorts.statistics import (
    difference_interval,
    wilson_interval,
)
from twin_market_flow.funnel_cohorts.storage import FunnelCohortStore
from twin_market_flow.funnel_cohorts.timeline import parse_ts, round6
from twin_market_flow.warehouse.run_ledger import DomainError, fingerprint, stable_json

MIN_MATURE_SUPPORT = 5
DIMENSIONS = ("overall", "channel", "enrollment_week", "week_channel")


def _load_input(input_path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DomainError("journey input must be a JSON object")
    for field_name in ("events", "identity_changes", "refunds", "cohort_policy"):
        if field_name not in data:
            raise DomainError(f"missing required field: {field_name}")
    policy = data["cohort_policy"]
    if policy.get("entry") != "first_visit":
        raise DomainError("only cohort_policy.entry=first_visit is supported")
    horizon = int(policy.get("horizon_days", 0))
    if horizon <= 0:
        raise DomainError("cohort_policy.horizon_days must be positive")
    if not policy.get("historical_cutoffs"):
        raise DomainError("cohort_policy.historical_cutoffs must not be empty")
    return data


def _segments(journeys: list[PersonJourney], horizon_days: int) -> list[dict[str, Any]]:
    segments = []
    for dimension in DIMENSIONS:
        grouped = segment_journeys(journeys, dimension)
        for key, members in grouped.items():
            funnel = build_funnel(members, horizon_days)
            time_to_convert = build_time_to_convert(members, horizon_days)
            segments.append(
                {
                    "dimension": dimension,
                    "segment": key,
                    "funnel": funnel,
                    "time_to_convert": time_to_convert,
                    "person_count": len(members),
                    "mature_persons": sum(1 for item in members if item.mature),
                    "not_mature_persons": sum(1 for item in members if not item.mature),
                }
            )
    return segments


def _gross_row(segment: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in segment["funnel"] if row["stage"] == "purchased_gross")


def _net_row(segment: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in segment["funnel"] if row["stage"] == "purchased_net")


def _immaturity_checks(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对比朴素占比与成熟风险集 KM 概率，量化观察期不足造成的偏差。"""
    checks = []
    for segment in segments:
        if segment["dimension"] != "channel":
            continue
        gross = _gross_row(segment)
        checks.append(
            {
                "segment": segment["segment"],
                "entered_persons": gross["entered_persons"],
                "mature_at_risk_persons": gross["mature_at_risk_persons"],
                "not_mature_persons": gross["not_mature_persons"],
                "naive_proportion_treating_immature_as_failures":
                    gross["naive_proportion_including_immature"]["point"],
                "mature_km_conversion_30d": gross["km_horizon_conversion"]["point"],
                "naive_minus_mature_gap": round6(
                    (gross["naive_proportion_including_immature"]["point"] or 0.0)
                    - (gross["km_horizon_conversion"]["point"] or 0.0)
                ),
                "interpretation": (
                    "naive proportion understates conversion when late purchases have not "
                    "had time to arrive; ranking must use the mature risk-set estimate"
                ),
            }
        )
    return checks


def _channel_cells(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = []
    for segment in segments:
        if segment["dimension"] != "channel":
            continue
        gross = _gross_row(segment)
        net = _net_row(segment)
        support = gross["mature_at_risk_persons"]
        cells.append(
            {
                "channel": segment["segment"],
                "entered_persons": gross["entered_persons"],
                "mature_at_risk_persons": support,
                "not_mature_persons": gross["not_mature_persons"],
                "gross_conversion_30d": gross["km_horizon_conversion"],
                "net_conversion_30d": net["km_horizon_conversion"],
                "naive_gross_proportion": gross["naive_proportion_including_immature"],
                "support_threshold": MIN_MATURE_SUPPORT,
                "supported_for_ranking": support >= MIN_MATURE_SUPPORT,
                "ranked": support >= MIN_MATURE_SUPPORT,
            }
        )
    return cells


def _week_cells(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = []
    for segment in segments:
        if segment["dimension"] not in ("enrollment_week", "week_channel"):
            continue
        gross = _gross_row(segment)
        support = gross["mature_at_risk_persons"]
        cells.append(
            {
                "dimension": segment["dimension"],
                "segment": segment["segment"],
                "entered_persons": gross["entered_persons"],
                "mature_at_risk_persons": support,
                "not_mature_persons": gross["not_mature_persons"],
                "gross_conversion_30d": gross["km_horizon_conversion"],
                "naive_gross_proportion": gross["naive_proportion_including_immature"],
                "supported_for_ranking": support >= MIN_MATURE_SUPPORT,
                "ranked": support >= MIN_MATURE_SUPPORT,
            }
        )
    return cells


def _pairwise_channel(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    channels = [item for item in segments if item["dimension"] == "channel"]
    comparisons = []
    for i, left in enumerate(channels):
        for right in channels[i + 1:]:
            left_gross = _gross_row(left)
            right_gross = _gross_row(right)
            diff = difference_interval(
                left_gross["mature_observed_events"],
                left_gross["mature_at_risk_persons"],
                right_gross["mature_observed_events"],
                right_gross["mature_at_risk_persons"],
            )
            unsupported = (
                left_gross["mature_at_risk_persons"] < MIN_MATURE_SUPPORT
                or right_gross["mature_at_risk_persons"] < MIN_MATURE_SUPPORT
            )
            comparisons.append(
                {
                    "left_channel": left["segment"],
                    "right_channel": right["segment"],
                    "basis": "30-day KM gross conversion on mature risk sets",
                    "difference_left_minus_right": diff,
                    "ci_excludes_zero": (
                        diff["ci_low"] is not None
                        and (diff["ci_low"] > 0 or diff["ci_high"] < 0)
                    ),
                    "supported_for_ranking": not unsupported,
                    "ranked": not unsupported,
                }
            )
    return comparisons


def _followup_imbalance(segments: list[dict[str, Any]]) -> dict[str, Any]:
    channels = [item for item in segments if item["dimension"] == "channel"]
    shares = {
        item["segment"]: round6(
            item["not_mature_persons"] / item["person_count"] if item["person_count"] else 0.0
        )
        for item in channels
    }
    values = [value for value in shares.values()]
    spread = round6(max(values) - min(values)) if values else 0.0
    return {
        "not_mature_share_by_channel": shares,
        "max_abs_share_spread": spread,
        "imbalance_flag": spread > 0.10,
        "rule": "channels whose immature shares differ by more than 10 percentage points are not directly comparable on naive proportions",
    }


def _cutoff_summary(run: dict[str, Any]) -> dict[str, Any]:
    """单个截止时点的摘要：成熟度、朴素口径与 KM 口径并列，暴露观察期偏差。"""
    result = run["result"]
    channel_rows = []
    for segment in result["funnels"]:
        if segment["dimension"] != "channel":
            continue
        gross = _gross_row(segment)
        net = _net_row(segment)
        channel_rows.append(
            {
                "channel": segment["segment"],
                "entered_persons": gross["entered_persons"],
                "mature_at_risk_persons": gross["mature_at_risk_persons"],
                "not_mature_persons": gross["not_mature_persons"],
                "gross_naive_proportion": gross["naive_proportion_including_immature"]["point"],
                "gross_km_conversion_30d": gross["km_horizon_conversion"]["point"],
                "gross_km_ci": [
                    gross["km_horizon_conversion"]["ci_low"],
                    gross["km_horizon_conversion"]["ci_high"],
                ],
                "net_km_conversion_30d": net["km_horizon_conversion"]["point"],
                # 无有效风险集（全部人物零随访）时不给估计、不得参与排名。
                "estimable": gross["km_detail"]["at_risk"] > 0
                and gross["km_horizon_conversion"]["point"] is not None,
                "supported_for_ranking": (
                    gross["mature_at_risk_persons"] >= MIN_MATURE_SUPPORT
                    and gross["km_horizon_conversion"]["point"] is not None
                ),
                "ranked": (
                    gross["mature_at_risk_persons"] >= MIN_MATURE_SUPPORT
                    and gross["km_horizon_conversion"]["point"] is not None
                ),
            }
        )
    overall = next(item for item in result["funnels"] if item["dimension"] == "overall")
    overall_gross = _gross_row(overall)
    return {
        "cohort_run_id": run["cohort_run_id"],
        "cutoff_at": result["cutoff_at"],
        "identity_version": result["identity_version"],
        "distinct_persons": result["identity"]["distinct_persons"],
        "overall": {
            "entered_persons": overall_gross["entered_persons"],
            "mature_at_risk_persons": overall_gross["mature_at_risk_persons"],
            "not_mature_persons": overall_gross["not_mature_persons"],
            "gross_naive_proportion": overall_gross["naive_proportion_including_immature"]["point"],
            "gross_km_conversion_30d": overall_gross["km_horizon_conversion"]["point"],
        },
        "channels": channel_rows,
    }


def _build_comparability(segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "support_rule": {
            "min_mature_at_risk_persons": MIN_MATURE_SUPPORT,
            "rule": "cells below the support threshold are returned with ranked=false and must not be ranked",
        },
        "channel_cells": _channel_cells(segments),
        "week_cells": _week_cells(segments),
        "pairwise_channel_differences": _pairwise_channel(segments),
        "follow_up_imbalance": _followup_imbalance(segments),
        "immaturity_bias_checks": _immaturity_checks(segments),
    }


def _endpoint_traces(journeys: list[PersonJourney]) -> dict[str, Any]:
    """退款与跨市场成交如何影响终点，逐单可追溯。"""
    cross_market_orders = []
    refunded = []
    for journey in journeys:
        for order in journey.gross_orders:
            if order["cross_market"]:
                cross_market_orders.append(
                    {
                        "person_id": journey.person_id,
                        "order_id": order["order_id"],
                        "entry_market": journey.entry_market,
                        "order_market": order["market"],
                        "convert_day": order["convert_day"],
                        "treatment": "counts as person-level conversion; market kept on the order for traceability",
                    }
                )
        for order in journey.refunded_orders:
            refunded.append(
                {
                    "person_id": journey.person_id,
                    "order_id": order["order_id"],
                    "market": order["market"],
                    "convert_day": order["convert_day"],
                    "refund_effective_at": order["refund_effective_at"],
                    "refund_known_at": order["refund_known_at"],
                    "treatment": "full refund inside the H-day window removes the order from purchased_net but keeps purchased_gross",
                }
            )
        for order in journey.post_window_refunded_orders:
            refunded.append(
                {
                    "person_id": journey.person_id,
                    "order_id": order["order_id"],
                    "market": order["market"],
                    "convert_day": order["convert_day"],
                    "refund_effective_at": order["refund_effective_at"],
                    "refund_known_at": order["refund_known_at"],
                    "refund_day_from_enrollment": order["refund_day_from_enrollment"],
                    "treatment": "refund effective after the H-day window stays traceable but cannot retroactively remove the 30-day net conversion",
                }
            )
    return {
        "cross_market_purchases": cross_market_orders,
        "refunded_orders": refunded,
        "gross_vs_net_rule": (
            "purchased_gross counts every in-window order; purchased_net excludes only orders fully "
            "refunded inside the window; refunds outside the window are listed but do not rewrite the "
            "net endpoint; refunds only affect their own order_id"
        ),
    }


def _journey_summaries(journeys: list[PersonJourney]) -> list[dict[str, Any]]:
    return [item.as_trace() for item in journeys]


def _run_for_cutoff(
    data: dict[str, Any],
    input_fingerprint: str,
    cutoff: str,
    identity: IdentityResolution,
    store: FunnelCohortStore,
    upstream_links: list[dict[str, Any]],
) -> dict[str, Any]:
    horizon_days = int(data["cohort_policy"]["horizon_days"])
    cutoff_dt = parse_ts(cutoff)
    journeys = build_person_journeys(
        data["events"], data["refunds"], identity, cutoff_dt, horizon_days
    )
    segments = _segments(journeys, horizon_days)
    overall = next(item for item in segments if item["dimension"] == "overall")

    result = {
        "cohort_run_id": "",  # 由 store 生成后回填
        "identity_version": identity.version_id,
        "cutoff_at": cutoff,
        "as_of": data["as_of"],
        "horizon_days": horizon_days,
        "entry_policy": data["cohort_policy"]["entry"],
        "input_fingerprint": input_fingerprint,
        "identity": {
            "version_id": identity.version_id,
            "effective_changes": identity.effective_changes,
            "active_merges": identity.active_merges,
            "registry": identity.registry,
            "distinct_persons": len(journeys),
            "explicitly_linked_persons": len(identity.person_devices),
        },
        "funnels": segments,
        "time_to_convert": overall["time_to_convert"],
        "endpoint_traces": _endpoint_traces(journeys),
        "person_journeys": _journey_summaries(journeys),
        "method": {
            "risk_set": "one record per person per endpoint; repeated visits across both markets never create duplicate persons",
            "repeated_observation": "each person contributes at most one event time per stage; later repeat visits update the journey but not the denominator",
            "incomplete_follow_up": (
                "persons without H observed days are right-censored at (cutoff - enrollment); "
                "they remain in the KM risk set and are never counted as failures"
            ),
            "estimator": "Kaplan-Meier cumulative incidence at horizon H with log-log 95% CI; Wilson CI for observed proportions",
            "late_arriving_facts": "events and refunds enter only after their available_at; earlier cutoff runs cannot see them",
            "no_randomized_experiment": "cohort funnel only; incrementality attribution in attribution/incrementality.py is untouched",
        },
    }
    result_fingerprint = fingerprint(
        {key: value for key, value in result.items() if key != "cohort_run_id"}
    )
    cohort_run_id = store.build_run_id(input_fingerprint, cutoff, identity.version_id)
    result["cohort_run_id"] = cohort_run_id
    store.save_run(
        {
            "cohort_run_id": cohort_run_id,
            "identity_version": identity.version_id,
            "cutoff_at": cutoff,
            "input_fingerprint": input_fingerprint,
            "identity_signature": identity.signature(),
            "result_fingerprint": result_fingerprint,
            "result": result,
        }
    )
    store.link_upstreams(cohort_run_id, upstream_links)
    stored = store.get_run(cohort_run_id)
    replayed = bool(stored and stored["result_fingerprint"] != result_fingerprint)
    return {
        "cohort_run_id": cohort_run_id,
        "result_fingerprint": result_fingerprint,
        "stored_fingerprint": stored["result_fingerprint"] if stored else result_fingerprint,
        "replayed_existing_record": replayed,
        "result": result,
        "journeys": journeys,
        "identity": identity,
    }


def _restatement(
    prior: dict[str, Any], restated: dict[str, Any]
) -> dict[str, Any]:
    """身份重述对历史口径的影响；旧版结果仍按原指纹保留，不被覆盖。"""
    prior_overall = next(
        item for item in prior["result"]["funnels"] if item["dimension"] == "overall"
    )
    restated_overall = next(
        item for item in restated["result"]["funnels"] if item["dimension"] == "overall"
    )

    def stage_pack(segments: dict[str, Any]) -> dict[str, Any]:
        pack = {}
        for row in segments["funnel"]:
            pack[row["stage"]] = {
                "entered_persons": row["entered_persons"],
                "mature_at_risk_persons": row["mature_at_risk_persons"],
                "not_mature_persons": row["not_mature_persons"],
                "conversion_30d": row["km_horizon_conversion"],
            }
        return pack

    prior_persons = {item["person_id"] for item in prior["result"]["person_journeys"]}
    restated_persons = {item["person_id"] for item in restated["result"]["person_journeys"]}
    return {
        "prior_identity_version": prior["identity"].version_id,
        "restated_identity_version": restated["identity"].version_id,
        "prior_cohort_run_id": prior["cohort_run_id"],
        "restated_cohort_run_id": restated["cohort_run_id"],
        "prior_result_fingerprint": prior["result_fingerprint"],
        "restated_result_fingerprint": restated["result_fingerprint"],
        "prior_distinct_persons": len(prior_persons),
        "restated_distinct_persons": len(restated_persons),
        "persons_added_by_restatement": sorted(restated_persons - prior_persons),
        "persons_removed_by_restatement": sorted(prior_persons - restated_persons),
        "stages_prior": stage_pack(prior_overall),
        "stages_restated": stage_pack(restated_overall),
        "historical_run_immutable": True,
        "rule": "the prior cutoff run keeps its original cohort_run_id and fingerprint; restatement is stored as a separate run",
    }


def run_analysis(
    input_path: str | Path, database: str | Path, output_path: str | Path | None = None
) -> dict[str, Any]:
    """执行完整队列漏斗分析并持久化，返回可 JSON 序列化的输出文档。"""
    data = _load_input(input_path)
    input_fingerprint = fingerprint(
        {
            "schema_version": data.get("schema_version"),
            "source_versions": data.get("source_versions"),
            "events": data["events"],
            "identity_changes": data["identity_changes"],
            "refunds": data["refunds"],
            "cohort_policy": data["cohort_policy"],
            "as_of": data["as_of"],
        }
    )
    as_of = parse_ts(data["as_of"])
    cutoffs = [
        value
        for value in data["cohort_policy"]["historical_cutoffs"]
        if parse_ts(value) <= as_of
    ]
    if not cutoffs:
        raise DomainError("no historical cutoff is on or before as_of")

    store = FunnelCohortStore(database)
    upstream_links = store.find_upstream_runs(data.get("baseline_case_id"))
    versions = distinct_identity_versions(data["identity_changes"], cutoffs)

    runs = []
    for cutoff, (version_id, identity) in zip(cutoffs, versions):
        runs.append(
            _run_for_cutoff(data, input_fingerprint, cutoff, identity, store, upstream_links)
        )

    primary = runs[-1]
    comparability = _build_comparability(primary["result"]["funnels"])
    cutoff_summaries = [_cutoff_summary(run) for run in runs]

    # 反例验证：把同一渠道在早截止时点的“朴素占比（未成熟当失败）”与
    # 成熟风险集 KM 概率并列，再与等满窗口后的结果对照。若渠道真的变差，
    # 成熟估计应同步走低；若只是晚到成交尚未到达，则只有朴素占比偏低。
    observation_window_proof = []
    for channel in sorted(
        {row["channel"] for summary in cutoff_summaries for row in summary["channels"]}
    ):
        series = []
        for summary in cutoff_summaries:
            row = next((item for item in summary["channels"] if item["channel"] == channel), None)
            if row is None:
                continue
            series.append(
                {
                    "cutoff_at": summary["cutoff_at"],
                    "mature_at_risk_persons": row["mature_at_risk_persons"],
                    "not_mature_persons": row["not_mature_persons"],
                    "naive_proportion": row["gross_naive_proportion"],
                    "mature_km_conversion_30d": row["gross_km_conversion_30d"],
                }
            )
        observation_window_proof.append({"channel": channel, "series": series})

    identity_effects: dict[str, Any] = {
        "registry": primary["identity"].registry,
        "versions": [
            {
                "identity_version": identity.version_id,
                "cutoff_at": cutoff,
                "effective_changes": identity.effective_changes,
                "distinct_persons": next(
                    run["result"]["identity"]["distinct_persons"]
                    for run in runs
                    if run["result"]["cutoff_at"] == cutoff
                ),
            }
            for cutoff, (_, identity) in zip(cutoffs, versions)
        ],
        "restatements": [],
        "stored_run_fingerprints": [
            {
                "cohort_run_id": run["cohort_run_id"],
                "result_fingerprint": run["result_fingerprint"],
                "stored_fingerprint": run["stored_fingerprint"],
                "immutable": run["result_fingerprint"] == run["stored_fingerprint"],
            }
            for run in runs
        ],
    }
    for prior, restated in zip(runs, runs[1:]):
        if prior["identity"].signature() != restated["identity"].signature():
            detail = _restatement(prior, restated)
            revision_id = store.save_revision(
                {
                    "identity_version": restated["identity"].version_id,
                    "prior_cohort_run_id": prior["cohort_run_id"],
                    "restated_cohort_run_id": restated["cohort_run_id"],
                    "change_ids": restated["identity"].effective_changes,
                    "detail": detail,
                }
            )
            detail["revision_id"] = revision_id
            identity_effects["restatements"].append(detail)

    output = {
        "cohort_run_id": primary["cohort_run_id"],
        "schema_version": 1,
        "as_of": data["as_of"],
        "horizon_days": int(data["cohort_policy"]["horizon_days"]),
        "run_catalog": [
            {
                "cohort_run_id": run["cohort_run_id"],
                "cutoff_at": run["result"]["cutoff_at"],
                "identity_version": run["identity"].version_id,
                "result_fingerprint": run["result_fingerprint"],
            }
            for run in runs
        ],
        "funnels": primary["result"]["funnels"],
        "time_to_convert": primary["result"]["time_to_convert"],
        "identity_effects": identity_effects,
        "comparability": comparability,
        "cutoff_summaries": cutoff_summaries,
        "observation_window_proof": observation_window_proof,
        "endpoint_traces": primary["result"]["endpoint_traces"],
        "person_journeys": primary["result"]["person_journeys"],
        "method": primary["result"]["method"],
        "sources": {
            "input_path": str(input_path),
            "input_fingerprint": input_fingerprint,
            "source_versions": data.get("source_versions", {}),
            "baseline_case_id": data.get("baseline_case_id"),
            "upstream_runs": upstream_links,
            "stages_linked": sorted({link["stage"] for link in upstream_links}),
            "cutoffs_evaluated": cutoffs,
            "stage_definitions": list(STAGES),
            "storage": {
                "database": str(database),
                "tables": [
                    "funnel_cohort_runs",
                    "funnel_run_upstreams",
                    "funnel_run_revisions",
                ],
            },
        },
    }

    if output_path is not None:
        Path(output_path).write_text(
            json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
    return output
