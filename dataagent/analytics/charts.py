"""Plotly-compatible chart JSON builders."""

from __future__ import annotations

from typing import Any

from dataagent.analytics.metrics import METRIC_REGISTRY


def build_chart(metric_result: dict[str, Any], *, chart_type: str | None = None) -> dict[str, Any]:
    metric = metric_result.get("metric", "")
    defn = METRIC_REGISTRY.get(metric)
    unit = metric_result.get("unit") or (defn.unit if defn else "")
    rows = metric_result.get("rows") or []
    empty = metric_result.get("empty_state")

    if empty and not rows:
        return {
            "data": [],
            "layout": {
                "title": metric,
                "annotations": [
                    {
                        "text": f"Unavailable: {empty}",
                        "xref": "paper",
                        "yref": "paper",
                        "showarrow": False,
                        "x": 0.5,
                        "y": 0.5,
                    }
                ],
            },
            "series_labels": [],
            "units": unit,
            "freshness": metric_result.get("freshness"),
            "empty_state": empty,
        }

    if metric == "session_volume":
        return _state_distribution(rows, metric, unit, metric_result)
    if metric == "ratings":
        return _ratings_chart(rows, metric, unit, metric_result)
    if metric.startswith("top_"):
        return _bar_ranking(rows, metric, unit, metric_result)
    if metric == "cost":
        return _cost_view(rows, metric, unit, metric_result)
    return _daily_trend(rows, metric, unit, metric_result)


def _daily_trend(rows, metric, unit, meta) -> dict[str, Any]:
    # group by dimension
    series: dict[str, dict[str, list]] = {}
    for r in rows:
        label = r.get("dimension_key") or metric
        s = series.setdefault(label, {"x": [], "y": []})
        s["x"].append(r["local_date"])
        s["y"].append(r.get("value"))
    data = [
        {"type": "scatter", "mode": "lines+markers", "name": name, "x": s["x"], "y": s["y"]}
        for name, s in sorted(series.items())
    ]
    return {
        "data": data,
        "layout": {"title": metric, "yaxis": {"title": unit}},
        "series_labels": sorted(series.keys()),
        "units": unit,
        "freshness": meta.get("freshness"),
        "empty_state": None,
    }


def _state_distribution(rows, metric, unit, meta) -> dict[str, Any]:
    # stacked by state over dates
    by_date: dict[str, dict[str, float]] = {}
    states: set[str] = set()
    for r in rows:
        if r["dimension_key"] == "__total__":
            continue
        states.add(r["dimension_key"])
        by_date.setdefault(r["local_date"], {})[r["dimension_key"]] = r.get("value") or 0
    dates = sorted(by_date.keys())
    data = []
    for state in sorted(states):
        data.append(
            {
                "type": "bar",
                "name": state,
                "x": dates,
                "y": [by_date[d].get(state, 0) for d in dates],
            }
        )
    return {
        "data": data,
        "layout": {"title": "Session state distribution", "barmode": "stack", "yaxis": {"title": unit}},
        "series_labels": sorted(states),
        "units": unit,
        "freshness": meta.get("freshness"),
        "empty_state": None,
    }


def _ratings_chart(rows, metric, unit, meta) -> dict[str, Any]:
    data = []
    labels = []
    for r in rows:
        scale = r["dimension_key"]
        labels.append(scale)
        hist = (r.get("payload") or {}).get("histogram") or {}
        data.append(
            {
                "type": "bar",
                "name": f"{scale} mean={r.get('value')}",
                "x": list(hist.keys()),
                "y": list(hist.values()),
            }
        )
    return {
        "data": data,
        "layout": {"title": "Ratings", "yaxis": {"title": "count"}},
        "series_labels": labels,
        "units": unit,
        "freshness": meta.get("freshness"),
        "empty_state": None,
    }


def _bar_ranking(rows, metric, unit, meta) -> dict[str, Any]:
    xs = [r["dimension_key"] for r in rows]
    ys = [r.get("value") for r in rows]
    return {
        "data": [{"type": "bar", "x": xs, "y": ys, "name": metric}],
        "layout": {"title": metric, "yaxis": {"title": unit}},
        "series_labels": [metric],
        "units": unit,
        "freshness": meta.get("freshness"),
        "empty_state": None,
    }


def _cost_view(rows, metric, unit, meta) -> dict[str, Any]:
    return _daily_trend(rows, metric, unit, meta)
