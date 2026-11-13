"""以访客入组时间为基准的转化漏斗队列分析。

模块只依赖 Python 3.11 标准库与既有的 SQLite 台账，不做活动随机实验，
也不替换 ``twin_market_flow.attribution`` 中已有的增量归因。

公开入口：

* :func:`run_analysis` —— 给定旅程 JSON 与数据库路径，重建漏斗并落库；
* :func:`main` —— ``python3 -m twin_market_flow.funnel_cohorts`` 命令行。
"""

from __future__ import annotations

from twin_market_flow.funnel_cohorts.analysis import run_analysis
from twin_market_flow.funnel_cohorts.cli import main

__all__ = ["run_analysis", "main"]
