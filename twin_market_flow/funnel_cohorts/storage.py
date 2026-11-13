"""漏斗队列运行的 SQLite 持久化与上游运行关联。

台账与既有 :class:`~twin_market_flow.warehouse.run_ledger.SQLiteLedger` 共用
同一个数据库文件，但使用独立的表，不改动原有 ``runs/events`` 结构：

* ``funnel_cohort_runs`` —— 内容寻址的分析运行（身份版本 + 截止时点 +
  输入指纹决定主键），重放同一输入返回同一 ``cohort_run_id``；
* ``funnel_run_upstreams`` —— 关联同库的 visitor_links、journey_sessionize
  等既有运行；
* ``funnel_run_revisions`` —— 仅登记同一身份版本被后续身份事实重述时的
  结果差异，历史运行记录本身保持不可变。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

from twin_market_flow.warehouse.run_ledger import stable_json, utc_now

NAMESPACE = uuid.UUID("6f1dc4a1-7e2e-4d3b-9a5f-2c8b1e0a9d34")
UPSTREAM_STAGE_MAP = {
    "visitor_links": "visitor_links",
    "journey_sessionize": "journey_sessionize",
}


class FunnelCohortStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS funnel_cohort_runs(
                    cohort_run_id TEXT PRIMARY KEY,
                    identity_version TEXT NOT NULL,
                    cutoff_at TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    identity_signature TEXT NOT NULL,
                    result_fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS funnel_cohort_runs_lookup
                    ON funnel_cohort_runs(input_fingerprint, cutoff_at, identity_version);
                CREATE TABLE IF NOT EXISTS funnel_run_upstreams(
                    cohort_run_id TEXT NOT NULL,
                    upstream_run_id TEXT NOT NULL,
                    upstream_case_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY(cohort_run_id, upstream_run_id, stage)
                );
                CREATE TABLE IF NOT EXISTS funnel_run_revisions(
                    revision_id TEXT PRIMARY KEY,
                    identity_version TEXT NOT NULL,
                    prior_cohort_run_id TEXT NOT NULL,
                    restated_cohort_run_id TEXT NOT NULL,
                    change_ids TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def build_run_id(
        input_fingerprint: str, cutoff_at: str, identity_version: str
    ) -> str:
        return "cohort-" + uuid.uuid5(
            NAMESPACE,
            stable_json(
                {
                    "input": input_fingerprint,
                    "cutoff": cutoff_at,
                    "identity_version": identity_version,
                }
            ),
        ).hex[:24]

    def save_run(self, record: Mapping[str, Any]) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO funnel_cohort_runs("
                "cohort_run_id,identity_version,cutoff_at,input_fingerprint,"
                "identity_signature,result_fingerprint,result_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    record["cohort_run_id"],
                    record["identity_version"],
                    record["cutoff_at"],
                    record["input_fingerprint"],
                    record["identity_signature"],
                    record["result_fingerprint"],
                    stable_json(record["result"]),
                    utc_now(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def get_run(self, cohort_run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT result_json, result_fingerprint FROM funnel_cohort_runs WHERE cohort_run_id=?",
                (cohort_run_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "result": json.loads(row["result_json"]),
            "result_fingerprint": row["result_fingerprint"],
        }

    def link_upstreams(self, cohort_run_id: str, links: list[Mapping[str, Any]]) -> None:
        connection = self._connect()
        try:
            for link in links:
                connection.execute(
                    "INSERT OR IGNORE INTO funnel_run_upstreams("
                    "cohort_run_id,upstream_run_id,upstream_case_id,stage,state,linked_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        cohort_run_id,
                        link["upstream_run_id"],
                        link["upstream_case_id"],
                        link["stage"],
                        link["state"],
                        utc_now(),
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def find_upstream_runs(self, case_id: str | None) -> list[dict[str, Any]]:
        """从既有 runs 表中找出 visitor_links / sessionize 运行并建立关联。"""
        connection = self._connect()
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            if exists is None:
                return []
            if case_id is None:
                rows = connection.execute(
                    "SELECT run_id, case_id, state, artifacts FROM runs ORDER BY updated_at ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT run_id, case_id, state, artifacts FROM runs WHERE case_id=? ORDER BY updated_at ASC",
                    (case_id,),
                ).fetchall()
            links: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                try:
                    artifacts = json.loads(row["artifacts"])
                except (TypeError, ValueError):
                    artifacts = {}
                for stage in UPSTREAM_STAGE_MAP.values():
                    if stage not in artifacts:
                        continue
                    key = (row["run_id"], stage)
                    if key in seen:
                        continue
                    seen.add(key)
                    links.append(
                        {
                            "upstream_run_id": row["run_id"],
                            "upstream_case_id": row["case_id"],
                            "stage": stage,
                            "state": row["state"],
                        }
                    )
            return links
        finally:
            connection.close()

    def save_revision(self, record: Mapping[str, Any]) -> str:
        revision_id = "rev-" + uuid.uuid5(
            NAMESPACE,
            stable_json(
                {
                    "identity_version": record["identity_version"],
                    "prior": record["prior_cohort_run_id"],
                    "restated": record["restated_cohort_run_id"],
                    "detail": record["detail"],
                }
            ),
        ).hex[:24]
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO funnel_run_revisions("
                "revision_id,identity_version,prior_cohort_run_id,restated_cohort_run_id,"
                "change_ids,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    revision_id,
                    record["identity_version"],
                    record["prior_cohort_run_id"],
                    record["restated_cohort_run_id"],
                    stable_json(record["change_ids"]),
                    stable_json(record["detail"]),
                    utc_now(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return revision_id
