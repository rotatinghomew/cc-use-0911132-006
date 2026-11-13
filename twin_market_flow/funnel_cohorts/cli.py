"""``python3 -m twin_market_flow.funnel_cohorts`` 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from twin_market_flow.funnel_cohorts.analysis import run_analysis
from twin_market_flow.warehouse.run_ledger import DomainError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="以访客入组时间为准的固定观察窗转化漏斗队列分析"
    )
    parser.add_argument("--input", required=True, help="旅程 JSON（events/identity_changes/refunds/cohort_policy）")
    parser.add_argument("--database", required=True, help="与 visitor_links、sessionize 共用的 SQLite 台账")
    parser.add_argument("--output", required=True, help="漏斗分析输出 JSON 路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not Path(args.input).is_file():
        print(f"funnel_cohorts 拒绝输入：输入文件不存在：{args.input}", file=sys.stderr)
        return 2
    try:
        output = run_analysis(args.input, args.database, args.output)
    except (DomainError, json.JSONDecodeError, OSError) as exc:
        print(f"funnel_cohorts 拒绝输入：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "cohort_run_id": output["cohort_run_id"],
                "output": args.output,
                "cutoffs_evaluated": output["sources"]["cutoffs_evaluated"],
                "upstream_stages_linked": output["sources"]["stages_linked"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
