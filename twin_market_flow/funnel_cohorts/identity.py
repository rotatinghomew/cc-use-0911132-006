"""时点身份解析（point-in-time identity resolution）。

``device_id`` 只是设备标识，不默认等同真人。身份事实以各自 ``available_at``
为界：在某个分析截止时点，只能采用当时已经可知的合并/撤回。

* merge：把若干设备归属到同一个 ``person_id``；
* split（可通过 ``supersedes`` 撤回某次 merge）：把列出的设备改挂到新的
  ``person_id``，其余设备保持原归属；
* 未出现在任何生效变更中的设备，以 ``device:<device_id>`` 作为独立自然人。

同一自然人反复进入两个市场只产生一条人物旅程，不会被重复计为多位新客；
同一终端在 split 之后按新的 ``person_id`` 归属，历史可在旧版本中保留、
在新版本中重述。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from twin_market_flow.funnel_cohorts.timeline import parse_ts
from twin_market_flow.warehouse.run_ledger import stable_json


@dataclass(slots=True)
class IdentityResolution:
    """单个截止时点上的身份口径。"""

    cutoff: str
    version_id: str
    effective_changes: list[str]
    device_to_person: dict[str, str]
    person_devices: dict[str, list[str]]
    active_merges: list[str] = field(default_factory=list)
    registry: list[dict[str, Any]] = field(default_factory=list)

    def person_of(self, device_id: str) -> str:
        return self.device_to_person.get(device_id, "device:" + device_id)

    def signature(self) -> str:
        """身份口径内容指纹，只取决于解析结果，与调用时传入的截止时点无关。"""
        payload = {
            "effective_changes": self.effective_changes,
            "device_to_person": dict(sorted(self.device_to_person.items())),
        }
        return stable_json(payload)


def _person_sort_key(person_id: str) -> tuple[int, str]:
    if person_id.startswith("person-"):
        try:
            return (0, f"{int(person_id.split('-', 1)[1]):08d}")
        except ValueError:
            return (0, person_id)
    return (1, person_id)


def resolve_identity(
    identity_changes: list[dict[str, Any]], cutoff: Any, version_id: str
) -> IdentityResolution:
    """按 ``cutoff`` 重放当时可知的身份变更，得到设备到人物的映射。

    变更按 (effective_at, available_at, change_id) 排序；只有
    ``available_at <= cutoff`` 的事实可采用。被后续 split 通过
    ``supersedes`` 撤回的 merge 不再生效。
    """
    cutoff_dt = parse_ts(cutoff) if isinstance(cutoff, str) else cutoff
    usable = []
    registry = []
    for change in identity_changes:
        available = parse_ts(change["available_at"])
        effective = parse_ts(change["effective_at"])
        known = available <= cutoff_dt
        registry.append(
            {
                "change_id": change["change_id"],
                "kind": change["kind"],
                "person_id": change["person_id"],
                "device_ids": list(change["device_ids"]),
                "supersedes": change.get("supersedes"),
                "effective_at": change["effective_at"],
                "available_at": change["available_at"],
                "known_at_cutoff": known,
            }
        )
        if not known:
            continue
        usable.append((effective, available, str(change["change_id"]), change))
    usable.sort(key=lambda item: (item[0], item[1], item[2]))

    superseded: set[str] = set()
    for _, _, _, change in usable:
        if change["kind"] == "split" and change.get("supersedes"):
            superseded.add(str(change["supersedes"]))

    # device -> 归属 person。重放规则：
    # * 未被撤回的 merge 直接建立归属；被撤回的 merge 不生效，但 split 会
    #   显式承接“原人物仍保留哪些设备”；
    # * split 只把它列出的设备改挂到新 person_id；同一次误合并中未被列出的
    #   设备保留在原人物下——同一终端后续确认属于不同人，不会把原人物的
    #   其他终端一并带走。
    device_to_person: dict[str, str] = {}
    effective: list[str] = []
    active_merges: list[str] = []
    merge_index = {
        str(change["change_id"]): change
        for _, _, _, change in usable
        if str(change["kind"]) == "merge"
    }
    for _, _, change_id, change in usable:
        kind = str(change["kind"])
        person_id = str(change["person_id"])
        # effective 记录该时点可知的全部身份事实（含后来撤回的历史事实）。
        effective.append(change_id)
        if kind == "merge":
            if change_id not in superseded:
                active_merges.append(change_id)
                for device_id in change["device_ids"]:
                    device_to_person[str(device_id)] = person_id
        elif kind == "split":
            supersedes_id = str(change.get("supersedes") or "")
            original = merge_index.get(supersedes_id)
            if original is not None:
                moved = {str(item) for item in change["device_ids"]}
                original_person = str(original["person_id"])
                for device_id in original["device_ids"]:
                    device_text = str(device_id)
                    if device_text not in moved:
                        device_to_person[device_text] = original_person
            for device_id in change["device_ids"]:
                device_to_person[str(device_id)] = person_id
        # 未知 kind 保持保守：不改动既有归属。

    person_devices: dict[str, list[str]] = {}
    for device_id, person_id in device_to_person.items():
        person_devices.setdefault(person_id, []).append(device_id)
    for devices in person_devices.values():
        devices.sort(key=lambda value: int(value.split("-")[-1]) if value.rsplit("-", 1)[-1].isdigit() else value)

    return IdentityResolution(
        cutoff=cutoff if isinstance(cutoff, str) else cutoff.isoformat(),
        version_id=version_id,
        effective_changes=effective,
        device_to_person=device_to_person,
        person_devices={key: person_devices[key] for key in sorted(person_devices, key=_person_sort_key)},
        active_merges=active_merges,
        registry=registry,
    )


def distinct_identity_versions(
    identity_changes: list[dict[str, Any]], cutoffs: list[str]
) -> list[tuple[str, IdentityResolution]]:
    """对一组截止时点去重，返回 (version_id, resolution)。

    身份口径相同的相邻时点共享同一身份版本 ID，便于表达“同一身份版本下、
    不同观察截止时点”的重算；口径发生变化才开新版本。
    """
    versions: list[tuple[str, IdentityResolution]] = []
    digest_to_version: dict[str, str] = {}
    serial = 0
    for cutoff in cutoffs:
        candidate = resolve_identity(identity_changes, cutoff, "")
        digest = candidate.signature()
        if digest not in digest_to_version:
            serial += 1
            version_id = f"iv{serial}"
            digest_to_version[digest] = version_id
            candidate.version_id = version_id
        else:
            candidate.version_id = digest_to_version[digest]
        versions.append((candidate.version_id, candidate))
    return versions
