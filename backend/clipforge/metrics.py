"""Prometheus metrics shared by the API and workers."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

JOBS_TOTAL = Counter("clipforge_jobs_total", "Jobs finished by a worker", ["kind", "outcome"])
JOB_SECONDS = Histogram(
    "clipforge_job_duration_seconds",
    "Wall-clock job duration",
    ["kind"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 2400, 3600, 7200),
)
HTTP_REQUESTS = Counter("clipforge_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram(
    "clipforge_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
