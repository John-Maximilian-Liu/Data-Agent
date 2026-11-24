"""Event ingestion — native, OTLP/HTTP, declarative JSON mapping."""

from dataagent.ingestion.pipeline import IngestResult, ingest_batch
from dataagent.ingestion.mapping import apply_mapping, dry_run_mapping

__all__ = ["IngestResult", "apply_mapping", "dry_run_mapping", "ingest_batch"]
