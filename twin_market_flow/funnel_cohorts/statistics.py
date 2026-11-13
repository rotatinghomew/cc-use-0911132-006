"""固定观察长度下的转化统计。

设计要点（对应业务口径）：

* 统一观察长度 :math:`H` 天（队列策略 ``horizon_days``，本项目为 30 天），
  不把尚未等满观察期的新客简单判失败；
* 风险集（risk set）按人计算：同一自然人在每个终点事件上至多贡献一次，
  反复到访不产生重复观察；
* 未在截止时点观察满 :math:`H` 天、或在窗口内尚未发生终点事件的人物按
  右删失处理，采用乘积极限（Kaplan–Meier）估计窗口内累计转化概率，
  并以 log-log 变换给出 95% 置信区间；
* 同时给出朴素占比（事件数 / 全部入组数，含未成熟）用于暴露
  “把未等满窗口当失败”的偏差，正式排名只使用成熟风险集结果。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from twin_market_flow.funnel_cohorts.timeline import round6

Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> dict[str, float]:
    """二项比例的 Wilson 置信区间；零样本时区间为 null。"""
    if trials <= 0:
        return {"point": None, "low": None, "high": None}
    phat = successes / trials
    denom = 1.0 + z * z / trials
    center = (phat + z * z / (2.0 * trials)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4.0 * trials)) / trials)) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return {"point": round6(phat), "low": round6(low), "high": round6(high)}


def kaplan_meier(records: Sequence[dict[str, Any]], horizon_days: int) -> dict[str, Any]:
    """乘积极限估计。

    每条记录形如 ``{"event_day": int|None, "censor_day": int}``：

    * 窗口内发生终点事件：``event_day`` 为入组后第几天（0..H），
      ``censor_day`` 取观察截止对应的删失天数（>= event_day）；
    * 未发生事件：``event_day=None``，``censor_day`` 为实际观察天数，
      未成熟人物 ``censor_day < H``，成熟而未转化人物 ``censor_day == H``。

    返回窗口内累计转化概率（= 1 - S(H)）、log-log 95% 区间、风险集轨迹、
    限定平均转化时间 RMST(0,H) 与可估计的中位转化天数。
    """
    horizon = int(horizon_days)
    at_risk = len(records)
    if at_risk == 0:
        return {
            "at_risk": 0,
            "events": 0,
            "censored": 0,
            "p_horizon": None,
            "ci_low": None,
            "ci_high": None,
            "survival_curve": [],
            "rmst_days": None,
            "median_convert_day": None,
        }

    # (day, events, censors)；事件与删失在同一天时先记事件后记删失。
    # censor_day<=0 表示入组当天即到观察截止、未贡献任何随访时间：不进入
    # KM 风险集（0 暴露人时），其人数由调用方的 not_mature 计数单独体现。
    buckets: dict[int, dict[str, int]] = {}
    zero_followup = 0
    for record in records:
        censor_day = min(int(record["censor_day"]), horizon)
        event_day = record.get("event_day")
        if event_day is not None and int(event_day) <= horizon:
            day = int(event_day)
            buckets.setdefault(day, {"events": 0, "censors": 0})["events"] += 1
        elif censor_day <= 0:
            zero_followup += 1
        else:
            buckets.setdefault(censor_day, {"events": 0, "censors": 0})["censors"] += 1

    effective_risk = at_risk - zero_followup
    if effective_risk == 0:
        # 全部人物零随访：没有任何信息估计转化概率，返回 None 而非 0，
        # 避免把“尚未观察”误读成“确定不转化”。
        return {
            "at_risk": at_risk,
            "events": 0,
            "censored": at_risk,
            "p_horizon": None,
            "ci_low": None,
            "ci_high": None,
            "survival_curve": [],
            "rmst_days": None,
            "median_convert_day": None,
        }

    n_risk = effective_risk
    survival = 1.0
    greenwood = 0.0
    curve: list[dict[str, Any]] = []
    rmst = 0.0
    prev_day = 0
    median_day: int | None = None
    total_events = 0
    total_censors = zero_followup
    sorted_days = sorted(buckets)
    for index, day in enumerate(sorted_days):
        bucket = buckets[day]
        # [prev_day, day) 区间内存活概率不变，计入 RMST；首个事件日之前
        # 全员存活（S=1），第一段从 day 0 按 survival=1 累计。
        rmst += survival * (day - prev_day)
        events = bucket["events"]
        censors = bucket["censors"]
        entry_risk = n_risk
        if events > 0:
            survival *= (entry_risk - events) / entry_risk
            # Greenwood 方差项 d / (n * (n - d))，在存活曲线上累加。
            greenwood += events / (entry_risk * max(entry_risk - events, 1))
            total_events += events
            if median_day is None and survival <= 0.5:
                median_day = day
        # 当天事件处理完后退出风险集的删失人物。
        n_risk -= events + censors
        total_censors += censors
        curve.append(
            {
                "day": day,
                "at_risk_start": entry_risk,
                "events": events,
                "censors": censors,
                "survival": round6(survival),
            }
        )
        # 下一段起点；若下一个桶正好落在 horizon（典型的成熟未转化删失），
        # 末段面积由循环的下一次迭代处理，这里不再追加，避免重复计算。
        prev_day = day
    # 最后一个桶之后到 H 的存活面积；桶已在 H 处时宽度为 0。
    if prev_day < horizon:
        rmst += survival * (horizon - prev_day)

    if survival <= 0.0:
        p_horizon = 1.0
        ci_low, ci_high = 1.0, 1.0
    else:
        p_horizon = 1.0 - survival
        if total_events == 0:
            # 无事件时仅能给出单侧 95% 上界（Peto 近似）。
            ci_low, ci_high = 0.0, min(1.0, 1.0 - math.exp(-(Z_95**2) / effective_risk))
        else:
            # log(-log S) 的标准误 = sqrt(Greenwood) / S（Kalbfleisch-Prentice）。
            se = math.sqrt(greenwood) / max(survival, 1e-12)
            logged = math.log(-math.log(max(survival, 1e-12)))
            upper_s = math.exp(-math.exp(logged - Z_95 * se))
            lower_s = math.exp(-math.exp(logged + Z_95 * se))
            ci_low = max(0.0, 1.0 - upper_s)
            ci_high = min(1.0, 1.0 - lower_s)

    return {
        "at_risk": at_risk,
        "events": total_events,
        "censored": total_censors,
        "p_horizon": round6(p_horizon),
        "ci_low": round6(ci_low),
        "ci_high": round6(ci_high),
        "survival_curve": curve,
        "rmst_days": round6(rmst),
        "median_convert_day": median_day,
    }


def difference_interval(
    successes_a: int,
    trials_a: int,
    successes_b: int,
    trials_b: int,
) -> dict[str, Any]:
    """两个独立比例之差（a - b）的正态近似 95% 区间。"""
    if trials_a <= 0 or trials_b <= 0:
        return {"difference": None, "ci_low": None, "ci_high": None}
    p_a = successes_a / trials_a
    p_b = successes_b / trials_b
    se = math.sqrt(
        p_a * (1.0 - p_a) / trials_a + p_b * (1.0 - p_b) / trials_b
    )
    diff = p_a - p_b
    return {
        "difference": round6(diff),
        "ci_low": round6(diff - Z_95 * se),
        "ci_high": round6(diff + Z_95 * se),
    }


def arrival_distribution(event_days: Sequence[int], horizon_days: int) -> dict[str, Any]:
    """到达时间分布：分位数、均值与直方图（按天，0..H）。"""
    days = sorted(int(day) for day in event_days if day is not None and int(day) <= horizon_days)
    if not days:
        return {
            "converters": 0,
            "mean_day": None,
            "p25_day": None,
            "p50_day": None,
            "p75_day": None,
            "min_day": None,
            "max_day": None,
            "histogram": [],
        }

    def quantile(q: float) -> float:
        if len(days) == 1:
            return float(days[0])
        pos = q * (len(days) - 1)
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return float(days[lo])
        return days[lo] + (days[hi] - days[lo]) * (pos - lo)

    counts: dict[int, int] = {}
    for day in days:
        counts[day] = counts.get(day, 0) + 1
    mean = sum(days) / len(days)
    # 均值在删失数据下有偏，仅在所有转化者均已观察到事件时作为描述量；
    # 不确定性由 KM/RMST 承担，这里给出事件日间的样本标准差作描述。
    if len(days) > 1:
        variance = sum((day - mean) ** 2 for day in days) / (len(days) - 1)
        std = math.sqrt(variance)
    else:
        std = 0.0
    return {
        "converters": len(days),
        "mean_day": round6(mean),
        "sample_std_day": round6(std),
        "p25_day": round6(quantile(0.25)),
        "p50_day": round6(quantile(0.50)),
        "p75_day": round6(quantile(0.75)),
        "min_day": days[0],
        "max_day": days[-1],
        "histogram": [
            {"day": day, "converters": counts[day]} for day in sorted(counts)
        ],
    }
