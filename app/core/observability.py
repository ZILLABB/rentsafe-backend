"""Request IDs, structured logs, and in-process metrics.

The app had none of this: a failure was visible only if a user reported it, and
the logs carried no way to tie a user's "it broke at 3pm" to the request that
broke. Three pieces, deliberately small:

* **Request IDs.** Every request gets one, echoed in ``X-Request-ID`` so a user
  can quote it and we can find the line. An inbound ID from a proxy is reused
  so a trace survives the hop.
* **Structured logs.** JSON in production (parseable by anything), human-
  readable in development. Access lines carry method, path, status and duration.
* **Metrics.** Prometheus text format at ``/metrics``. Counters and a latency
  histogram, held in memory.

What this deliberately does *not* do is log request bodies, query strings, or
any field that could carry review text or a phone number. The whole product is
built on tenants saying things they would not put their name to; log lines are
copied to more places than a database is.

Metrics are per-process, so with several workers a scrape sees one worker's
view. That is the normal shape for Prometheus (each worker is its own target)
and only misleads if someone points a single scrape at a load-balanced address.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from threading import Lock
from typing import ClassVar

# Set per request so any log line emitted while handling it can be correlated,
# without threading an argument through every function.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Paths whose cardinality would explode a metrics label set: anything with an
# ID in it. Templated route paths are used instead (see `route_template`).
_UNLABELLED = "<other>"


class RequestIdFilter(logging.Filter):
    """Attaches the current request ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a log shipper to parse.

    Extra fields set via ``logger.info(..., extra={...})`` are included, so an
    access line can carry status and duration as real fields rather than as
    text someone later has to regex out of a message.
    """

    _BUILTIN: ClassVar[set[str]] = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN and key != "request_id":
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class Metrics:
    """Counters and latency buckets, safe to touch from several threads.

    Deliberately not a Prometheus client dependency: the surface needed here is
    two counters and a histogram, and the exposition format is a dozen lines to
    render.
    """

    # Seconds. Chosen around what this app actually does: most reads are single
    # queries, address search reaches a rate-limited upstream and is slow.
    BUCKETS: ClassVar[tuple[float, ...]] = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self) -> None:
        self._lock = Lock()
        self.requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self.latency: dict[tuple[str, str], list[int]] = defaultdict(
            lambda: [0] * (len(self.BUCKETS) + 1)
        )
        self.latency_sum: dict[tuple[str, str], float] = defaultdict(float)
        self.exceptions: dict[str, int] = defaultdict(int)

    def observe(self, method: str, path: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests[(method, path, status)] += 1
            buckets = self.latency[(method, path)]
            for i, edge in enumerate(self.BUCKETS):
                if seconds <= edge:
                    buckets[i] += 1
                    break
            else:
                buckets[-1] += 1
            self.latency_sum[(method, path)] += seconds

    def record_exception(self, kind: str) -> None:
        with self._lock:
            self.exceptions[kind] += 1

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            lines.append("# HELP rentsafe_requests_total Requests by method, route and status.")
            lines.append("# TYPE rentsafe_requests_total counter")
            for (method, path, status), count in sorted(self.requests.items()):
                lines.append(
                    f'rentsafe_requests_total{{method="{method}",route="{path}",'
                    f'status="{status}"}} {count}'
                )

            lines.append("# HELP rentsafe_request_seconds Request latency.")
            lines.append("# TYPE rentsafe_request_seconds histogram")
            for (method, path), buckets in sorted(self.latency.items()):
                cumulative = 0
                for i, edge in enumerate(self.BUCKETS):
                    cumulative += buckets[i]
                    lines.append(
                        f'rentsafe_request_seconds_bucket{{method="{method}",'
                        f'route="{path}",le="{edge}"}} {cumulative}'
                    )
                cumulative += buckets[-1]
                lines.append(
                    f'rentsafe_request_seconds_bucket{{method="{method}",'
                    f'route="{path}",le="+Inf"}} {cumulative}'
                )
                lines.append(
                    f'rentsafe_request_seconds_count{{method="{method}",'
                    f'route="{path}"}} {cumulative}'
                )
                lines.append(
                    f'rentsafe_request_seconds_sum{{method="{method}",'
                    f'route="{path}"}} {self.latency_sum[(method, path)]:.6f}'
                )

            lines.append("# HELP rentsafe_exceptions_total Unhandled exceptions by type.")
            lines.append("# TYPE rentsafe_exceptions_total counter")
            for kind, count in sorted(self.exceptions.items()):
                lines.append(f'rentsafe_exceptions_total{{type="{kind}"}} {count}')

        return "\n".join(lines) + "\n"


metrics = Metrics()


def route_template(request) -> str:
    """The templated path (``/properties/{property_id}``), not the concrete one.

    Labelling metrics with the concrete path would mint a new time series per
    property and blow up the scrape. Unmatched paths collapse to a single label
    so a 404 scanner can't do the same thing.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or _UNLABELLED


def new_request_id(inbound: str | None) -> str:
    """Reuse a proxy's ID when it looks sane, otherwise mint one.

    The inbound value is echoed into responses and logs, so it is length-capped
    and stripped of anything that could break a log line or a header.
    """
    if inbound:
        cleaned = "".join(c for c in inbound.strip() if c.isalnum() or c in "-_.")[:64]
        if cleaned:
            return cleaned
    return uuid.uuid4().hex[:16]


def configure_logging(*, debug: bool) -> None:
    """Install the formatter and request-ID filter on the root handler."""
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if debug:
        handler.setFormatter(
            logging.Formatter("%(levelname)s [%(name)s] (%(request_id)s) %(message)s")
        )
    else:
        handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    # Replace rather than append: basicConfig may already have added one, and
    # two handlers means every line is emitted twice.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.INFO if debug else logging.WARNING)
    # Application logs stay at INFO in production — they carry no user content
    # and are the only operational signal until tracing is wired.
    logging.getLogger("app").setLevel(logging.INFO)

    # httpx logs the full request URL at INFO, and we call Nominatim with the
    # user's typed home address in the query string. At the default production
    # level (WARNING) that is already silent, but the whole point of pinning it
    # here is that it stays silent if someone later raises the root level to
    # debug an unrelated problem. Same for urllib3 and botocore.
    for chatty in ("httpx", "httpcore", "urllib3", "botocore", "boto3", "s3transfer"):
        logging.getLogger(chatty).setLevel(logging.WARNING)
