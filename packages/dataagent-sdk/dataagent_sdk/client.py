"""Sync and async SDK clients with bounded retry on idempotent ops."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from dataagent_sdk.errors import raise_for_status


class _Base:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        credential: str | None = None,
        principal: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.credential = credential
        self.principal = principal
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"X-Request-Id": str(uuid.uuid4()), "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.credential:
            h["X-Dataagent-Credential"] = self.credential
        if self.principal:
            h["X-Dataagent-Principal"] = self.principal
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h


class Client(_Base):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        self.events = _Events(self)
        self.analytics = _Analytics(self)
        self.benchmarks = _Benchmarks(self)
        self.experiments = _Experiments(self)
        self.agents = _Agents(self)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        idempotency_key: str | None = None,
        idempotent: bool = False,
    ) -> Any:
        headers = self._headers(idempotency_key)
        attempt = 0
        while True:
            attempt += 1
            resp = self._client.request(method, path, json=json, params=params, headers=headers)
            request_id = resp.headers.get("X-Request-Id")
            try:
                body = resp.json() if resp.content else {}
            except Exception:
                body = {"code": "invalid_json", "message": resp.text[:200]}
            if resp.status_code in (429, 502, 503, 504) and idempotent and attempt <= self.max_retries:
                time.sleep(min(2**attempt, 8))
                continue
            if isinstance(body.get("detail"), dict):
                body = {**body, **body["detail"]}
            raise_for_status(resp.status_code, body if isinstance(body, dict) else {}, request_id)
            return body

    def as_tools(self, project_id: str) -> list[dict[str, Any]]:
        """JSON Schema tool definitions + handlers bound to active identity."""

        def handle(name: str, args: dict[str, Any]) -> Any:
            if name == "list_metrics":
                return self.analytics.metrics(
                    project_id,
                    metric=args["metric"],
                    start=args["start"],
                    end=args["end"],
                    agent_ids=args.get("agent_ids"),
                )
            if name == "get_chart":
                return self.analytics.charts(
                    project_id,
                    metric=args["metric"],
                    start=args["start"],
                    end=args["end"],
                )
            if name == "ingest_events":
                return self.events.batch(
                    project_id,
                    source_id=args["source_id"],
                    events=args["events"],
                    idempotency_key=args.get("idempotency_key"),
                )
            if name == "import_qa":
                return self.benchmarks.qa_import(
                    project_id,
                    args["benchmark_id"],
                    content=args["content"],
                    format=args.get("format", "jsonl"),
                    validate_only=args.get("validate_only", False),
                )
            if name == "start_experiment":
                return self.experiments.create(project_id, args)
            if name == "get_report":
                return self.experiments.report(project_id, args["experiment_id"])
            raise ValueError(f"unknown tool {name}")

        tools = [
            {
                "name": "list_metrics",
                "description": "Read authorized analytics metrics (default read).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "agent_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["metric", "start", "end"],
                },
                "mutation": False,
                "handler": lambda args: handle("list_metrics", args),
            },
            {
                "name": "get_chart",
                "description": "Fetch Plotly-compatible chart JSON.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["metric", "start", "end"],
                },
                "mutation": False,
                "handler": lambda args: handle("get_chart", args),
            },
            {
                "name": "ingest_events",
                "description": "Batch ingest events (requires source.write).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "events": {"type": "array"},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["source_id", "events"],
                },
                "mutation": True,
                "required_scope": "source.write",
                "handler": lambda args: handle("ingest_events", args),
            },
            {
                "name": "import_qa",
                "description": "Import seed QA into a benchmark (mutation).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "benchmark_id": {"type": "string"},
                        "content": {"type": "string"},
                        "format": {"type": "string"},
                        "validate_only": {"type": "boolean"},
                    },
                    "required": ["benchmark_id", "content"],
                },
                "mutation": True,
                "required_scope": "benchmark.import",
                "handler": lambda args: handle("import_qa", args),
            },
            {
                "name": "start_experiment",
                "description": "Start a release comparison experiment (mutation).",
                "parameters": {"type": "object", "additionalProperties": True},
                "mutation": True,
                "required_scope": "experiment.create",
                "handler": lambda args: handle("start_experiment", args),
            },
            {
                "name": "get_report",
                "description": "Retrieve experiment report.",
                "parameters": {
                    "type": "object",
                    "properties": {"experiment_id": {"type": "string"}},
                    "required": ["experiment_id"],
                },
                "mutation": False,
                "handler": lambda args: handle("get_report", args),
            },
        ]
        return tools


class AsyncClient(_Base):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        idempotency_key: str | None = None,
        idempotent: bool = False,
    ) -> Any:
        headers = self._headers(idempotency_key)
        attempt = 0
        while True:
            attempt += 1
            resp = await self._client.request(
                method, path, json=json, params=params, headers=headers
            )
            request_id = resp.headers.get("X-Request-Id")
            body = resp.json() if resp.content else {}
            if resp.status_code in (429, 502, 503, 504) and idempotent and attempt <= self.max_retries:
                import asyncio

                await asyncio.sleep(min(2**attempt, 8))
                continue
            if isinstance(body.get("detail"), dict):
                body = {**body, **body["detail"]}
            raise_for_status(resp.status_code, body if isinstance(body, dict) else {}, request_id)
            return body


class _Events:
    def __init__(self, client: Client):
        self._c = client

    def batch(
        self,
        project_id: str,
        *,
        source_id: str,
        events: list[dict],
        idempotency_key: str | None = None,
    ) -> Any:
        return self._c.request(
            "POST",
            f"/v1/projects/{project_id}/events:batch",
            json={"events": events},
            params={"source_id": source_id},
            idempotency_key=idempotency_key or str(uuid.uuid4()),
            idempotent=True,
        )


class _Analytics:
    def __init__(self, client: Client):
        self._c = client

    def metrics(self, project_id: str, *, metric: str, start: str, end: str, agent_ids=None) -> Any:
        params: dict[str, Any] = {"metric": metric, "start": start, "end": end}
        if agent_ids:
            params["agent_ids"] = agent_ids
        return self._c.request("GET", f"/v1/projects/{project_id}/analytics/metrics", params=params)

    def charts(self, project_id: str, *, metric: str, start: str, end: str) -> Any:
        return self._c.request(
            "GET",
            f"/v1/projects/{project_id}/analytics/charts/{metric}",
            params={"start": start, "end": end},
        )


class _Benchmarks:
    def __init__(self, client: Client):
        self._c = client

    def qa_import(self, project_id: str, benchmark_id: str, **body) -> Any:
        return self._c.request(
            "POST",
            f"/v1/projects/{project_id}/benchmarks/{benchmark_id}/qa-imports",
            json=body,
            idempotent=False,
        )

    def refresh(self, project_id: str, benchmark_id: str, **body) -> Any:
        return self._c.request(
            "POST",
            f"/v1/projects/{project_id}/benchmarks/{benchmark_id}/refreshes",
            json=body,
        )


class _Experiments:
    def __init__(self, client: Client):
        self._c = client

    def create(self, project_id: str, body: dict) -> Any:
        return self._c.request("POST", f"/v1/projects/{project_id}/experiments", json=body)

    def results(self, project_id: str, experiment_id: str) -> Any:
        return self._c.request(
            "GET", f"/v1/projects/{project_id}/experiments/{experiment_id}/results"
        )

    def report(self, project_id: str, experiment_id: str) -> Any:
        return self._c.request(
            "GET", f"/v1/projects/{project_id}/experiments/{experiment_id}/report"
        )

    def cancel(self, project_id: str, experiment_id: str) -> Any:
        return self._c.request(
            "POST", f"/v1/projects/{project_id}/experiments/{experiment_id}/cancel"
        )


class _Agents:
    def __init__(self, client: Client):
        self._c = client

    def list(self, project_id: str, **params) -> Any:
        return self._c.request("GET", f"/v1/projects/{project_id}/agents", params=params)

    def create(self, project_id: str, body: dict) -> Any:
        return self._c.request("POST", f"/v1/projects/{project_id}/agents", json=body)
