"""Declarative JSON mapping — data-driven transforms, no user-supplied Python."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class MappingError(Exception):
    def __init__(self, code: str, message: str, path: str | None = None):
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)


def _get_path(obj: Any, path: str) -> Any:
    """Dot-path with optional [index] segments."""
    cur = obj
    for part in path.replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _convert_timestamp(value: Any, unit: str | None = None) -> str:
    if value is None:
        raise MappingError("missing_timestamp", "timestamp required")
    if isinstance(value, (int, float)):
        unit = unit or "ms"
        if unit == "ns":
            seconds = value / 1_000_000_000
        elif unit == "us":
            seconds = value / 1_000_000
        elif unit == "s":
            seconds = value
        else:
            seconds = value / 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    if isinstance(value, str):
        return value
    raise MappingError("bad_timestamp", f"unsupported timestamp type {type(value)}")


def _enum_transform(value: Any, mapping: dict[str, str]) -> str | None:
    if value is None:
        return None
    key = str(value)
    if key not in mapping:
        raise MappingError("enum_unknown", f"value {key} not in enum map")
    return mapping[key]


def apply_mapping(raw: dict[str, Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """
    mapping schema:
      fields: { canonical_field: { path, optional?, enum?, timestamp_unit?, default? } }
      expand: optional array path producing multiple events
      event_type: static or { path }
    """
    expand_path = mapping.get("expand")
    if expand_path:
        items = _get_path(raw, expand_path)
        if not isinstance(items, list):
            raise MappingError("expand_not_array", "expand path is not an array", expand_path)
        results = []
        for idx, item in enumerate(items):
            wrapped = {**raw, "_item": item, "_index": idx}
            results.append(_map_one(wrapped, mapping, item_root=item))
        return results
    return [_map_one(raw, mapping)]


def _map_one(
    raw: dict[str, Any], mapping: dict[str, Any], item_root: Any | None = None
) -> dict[str, Any]:
    fields_spec: dict[str, Any] = mapping.get("fields") or {}
    out: dict[str, Any] = {}
    root = item_root if item_root is not None else raw

    event_type = mapping.get("event_type")
    if isinstance(event_type, dict):
        out["event_type"] = _get_path(root, event_type["path"])
    elif event_type:
        out["event_type"] = event_type

    for dest, spec in fields_spec.items():
        if isinstance(spec, str):
            spec = {"path": spec}
        path = spec["path"]
        # Prefer item root for relative paths starting without $
        source = raw if path.startswith("$") else root
        path_clean = path[1:] if path.startswith("$") else path
        value = _get_path(source if path.startswith("$") else root, path_clean)
        if value is None:
            if "default" in spec:
                value = spec["default"]
            elif spec.get("optional"):
                continue
            else:
                raise MappingError("missing_field", f"required field {dest}", path)
        if "enum" in spec:
            value = _enum_transform(value, spec["enum"])
        if spec.get("timestamp"):
            value = _convert_timestamp(value, spec.get("timestamp_unit"))
        if spec.get("unit_scale"):
            value = float(value) * float(spec["unit_scale"])
        out[dest] = value

    if "schema_version" not in out:
        out["schema_version"] = mapping.get("schema_version", "1.0")
    if "event_id" not in out:
        # deterministic from paths if configured
        eid = mapping.get("event_id_path")
        if eid:
            out["event_id"] = str(_get_path(raw, eid))
    return out


def dry_run_mapping(
    samples: list[dict[str, Any]], mapping: dict[str, Any]
) -> dict[str, Any]:
    accepted = []
    rejected = []
    for i, sample in enumerate(samples):
        try:
            events = apply_mapping(sample, mapping)
            accepted.append({"row": i, "events": events})
        except MappingError as e:
            rejected.append(
                {"row": i, "code": e.code, "message": e.message, "path": e.path}
            )
    return {
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted[:20],
        "rejected": rejected[:20],
        "diagnostic_codes": sorted({r["code"] for r in rejected}),
    }
