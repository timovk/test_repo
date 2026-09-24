"""Download the REAL source datasets (CBS / PDOK open data) into ``data/raw``.

Every source configured in ``config/geography.yaml`` is fetched once (streaming, with retries and
exponential backoff), hashed (SHA-256) and recorded in ``data/raw/sources_<year>.json`` together with
its URL, size, retrieval time, publisher and license.  Files that are already present are *not*
downloaded again unless ``force=True`` (the 220 MB Wijk- en Buurtkaart GeoPackage in particular).

Three fetch strategies exist (``SourceSpec.resolved_kind``):

* ``file`` — plain streamed download (the GeoPackage);
* ``wfs_geojson`` — a WFS ``GetFeature`` GeoJSON request, paged with ``startIndex``/``count`` when the
  server caps the number of returned features;
* ``odata`` — a CBS StatLine ODataFeed; pages linked through ``odata.nextLink`` (10 000 rows each)
  are merged into a single JSON document ``{"value": [...]}``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.core.errors import DownloadError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.settings import get_settings
from app.geography.config import GeographyConfig, SourceSpec, load_geography_config

log = get_logger(__name__)

T = TypeVar("T")

#: Retry policy for transient network failures.
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 60.0
#: Hard stop for runaway pagination.
MAX_PAGES = 1000
USER_AGENT = "nlfed-geography/1.0 (NL Federal Election Simulator; open-data pipeline)"


@dataclass
class DownloadRecord:
    """Provenance of one downloaded (or already present) source file."""

    key: str
    name: str
    publisher: str
    url: str
    license: str
    path: str
    sha256: str
    size_bytes: int
    retrieved_at: str  # ISO-8601 UTC
    vintage: int
    kind: str
    downloaded: bool  # False when an existing file was reused

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["bytes"] = out.pop("size_bytes")
        return out

    @classmethod
    def from_json(cls, key: str, data: dict[str, Any]) -> DownloadRecord:
        return cls(
            key=key,
            name=str(data.get("name", key)),
            publisher=str(data.get("publisher", "")),
            url=str(data.get("url", "")),
            license=str(data.get("license", "")),
            path=str(data.get("path", "")),
            sha256=str(data.get("sha256", "")),
            size_bytes=int(data.get("bytes", data.get("size_bytes", 0)) or 0),
            retrieved_at=str(data.get("retrieved_at", "")),
            vintage=int(data.get("vintage", 0) or 0),
            kind=str(data.get("kind", "file")),
            downloaded=bool(data.get("downloaded", False)),
        )


# --------------------------------------------------------------------------- paths / manifest
def raw_path(source: SourceSpec, year: int) -> Path:
    """Local path of a source file for geography ``year``."""
    return get_settings().raw_dir / source.filename_for(year)


def sources_manifest_path(year: int) -> Path:
    return get_settings().raw_dir / f"sources_{year}.json"


def read_sources_manifest(year: int) -> dict[str, DownloadRecord]:
    """Records of ``data/raw/sources_<year>.json`` (empty when absent)."""
    path = sources_manifest_path(year)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: DownloadRecord.from_json(k, v) for k, v in payload.get("sources", {}).items()}


def _tmp_path(dest: Path) -> Path:
    """Unique sibling temporary path (concurrent writers never share a partial file)."""
    return dest.with_name(f".{dest.name}.{os.getpid()}-{threading.get_ident()}.part")


def _atomic_write_bytes(dest: Path, payload: bytes) -> None:
    tmp = _tmp_path(dest)
    try:
        tmp.write_bytes(payload)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def _write_sources_manifest(year: int, records: Iterable[DownloadRecord]) -> Path:
    """Merge ``records`` into ``sources_<year>.json``.  The file is only rewritten when a record
    changed, so the frequent no-op calls made by every build leave it untouched."""
    path = sources_manifest_path(year)
    before = read_sources_manifest(year)
    merged = dict(before)
    for rec in records:
        merged[rec.key] = rec
    sources = {k: merged[k].to_json() for k in sorted(merged)}
    if path.exists() and sources == {k: before[k].to_json() for k in sorted(before)}:
        return path
    payload = {"year": year, "updated_at": _now_iso(), "sources": sources}
    _atomic_write_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def _display_path(path: Path) -> str:
    """Path relative to the project root when possible (portable provenance records)."""
    try:
        return path.resolve().relative_to(get_settings().project_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    """Streaming SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- HTTP helpers
def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in (408, 425, 429)
    return isinstance(exc, httpx.TransportError | _IncompleteDownloadError | json.JSONDecodeError)


class _IncompleteDownloadError(Exception):
    """Fewer bytes arrived than announced by Content-Length."""


def with_retries(
    fn: Callable[[], T],
    label: str,
    attempts: int = MAX_ATTEMPTS,
    base_delay: float = BACKOFF_BASE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` retrying transient HTTP/network errors with exponential backoff.

    Non-retryable errors (4xx other than 408/425/429) raise :class:`DownloadError` immediately.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not _retryable(exc):
                raise DownloadError(f"{label}: {exc}") from exc
            last = exc
            if attempt == attempts:
                break
            delay = min(BACKOFF_MAX_S, base_delay * 2 ** (attempt - 1))
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.0fs",
                label,
                attempt,
                attempts,
                exc,
                delay,
                extra=log_ctx(attempt=attempt, delay_s=delay),
            )
            sleep(delay)
    raise DownloadError(f"{label}: giving up after {attempts} attempts: {last}") from last


def _client() -> httpx.Client:
    settings = get_settings()
    timeout = httpx.Timeout(settings.http_timeout_s, connect=30.0)
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _stream_to_file(client: httpx.Client, url: str, dest: Path, label: str) -> tuple[str, int]:
    """Stream ``url`` into ``dest`` atomically; return (sha256, bytes)."""
    tmp = _tmp_path(dest)
    h = hashlib.sha256()
    received = 0
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            next_report = 0.1
            t0 = time.perf_counter()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
                    h.update(chunk)
                    received += len(chunk)
                    if total and received / total >= next_report:
                        rate = received / max(time.perf_counter() - t0, 1e-6) / 1e6
                        log.info(
                            "%s: %3.0f%% (%.1f / %.1f MB, %.1f MB/s)",
                            label,
                            100 * received / total,
                            received / 1e6,
                            total / 1e6,
                            rate,
                        )
                        next_report += 0.1
            if total and received != total:
                raise _IncompleteDownloadError(f"received {received} of {total} bytes")
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return h.hexdigest(), received


def _write_bytes(dest: Path, payload: bytes) -> tuple[str, int]:
    _atomic_write_bytes(dest, payload)
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _get_json(client: httpx.Client, url: str) -> Any:
    resp = client.get(url)
    resp.raise_for_status()
    return resp.json()


def _with_query(url: str, **params: Any) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query.extend((k, str(v)) for k, v in params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, safe=":$,/"), parts.fragment))


def fetch_odata(client: httpx.Client, url: str, label: str) -> dict[str, Any]:
    """Fetch all pages of a CBS ODataFeed (following ``odata.nextLink``) into one document."""
    rows: list[Any] = []
    metadata = None
    next_url: str | None = url
    pages = 0
    while next_url:
        if pages >= MAX_PAGES:
            raise DownloadError(f"{label}: more than {MAX_PAGES} OData pages — aborting")
        page_url = next_url
        page = with_retries(lambda u=page_url: _get_json(client, u), f"{label} page {pages + 1}")
        if not isinstance(page, dict) or "value" not in page:
            raise DownloadError(f"{label}: unexpected OData payload (no 'value')")
        metadata = metadata or page.get("odata.metadata")
        rows.extend(page["value"])
        pages += 1
        next_url = page.get("odata.nextLink")
        log.info("%s: page %d (%d rows so far)", label, pages, len(rows))
    return {"odata.metadata": metadata, "source_url": url, "pages": pages, "value": rows}


def fetch_wfs_geojson(client: httpx.Client, url: str, label: str, page_size: int = 1000) -> dict[str, Any]:
    """Fetch a WFS GeoJSON FeatureCollection, paging when ``numberMatched`` exceeds the first page."""
    first = with_retries(lambda: _get_json(client, url), label)
    if not isinstance(first, dict) or first.get("type") != "FeatureCollection":
        raise DownloadError(f"{label}: response is not a GeoJSON FeatureCollection")
    features = list(first.get("features") or [])
    matched = first.get("numberMatched")
    if isinstance(matched, int) and matched > len(features):
        while len(features) < matched:
            if len(features) // max(page_size, 1) > MAX_PAGES:
                raise DownloadError(f"{label}: runaway WFS paging")
            page_url = _with_query(url, count=page_size, startIndex=len(features))
            page = with_retries(lambda u=page_url: _get_json(client, u), f"{label} @{len(features)}")
            batch = page.get("features") or []
            if not batch:
                break
            features.extend(batch)
            log.info("%s: %d / %d features", label, len(features), matched)
        if len(features) != matched:
            raise DownloadError(f"{label}: received {len(features)} of {matched} features")
    first["features"] = features
    first["numberReturned"] = len(features)
    return first


# --------------------------------------------------------------------------- public API
def download_source(
    key: str,
    source: SourceSpec,
    year: int,
    force: bool = False,
    client: httpx.Client | None = None,
) -> DownloadRecord:
    """Download (or reuse) one configured source and return its provenance record."""
    settings = get_settings()
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_path(source, year)
    url = source.url_for(year)
    kind = source.resolved_kind()
    label = f"download {key}"
    previous = read_sources_manifest(year).get(key)

    if dest.exists() and not force:
        size = dest.stat().st_size
        with Timer(log, f"sha256 {dest.name}", level=logging.DEBUG):
            sha = sha256_file(dest)
        same = previous is not None and previous.sha256 == sha and previous.url == url
        retrieved = (previous.retrieved_at if same and previous else "") or _mtime_iso(dest)
        log.info("%s: present (%s, %.1f MB) — skipped", label, dest.name, size / 1e6)
        return DownloadRecord(
            key=key,
            name=source.name_for(year),
            publisher=source.publisher,
            url=url,
            license=source.license,
            path=_display_path(dest),
            sha256=sha,
            size_bytes=size,
            retrieved_at=retrieved,
            vintage=source.vintage_for(year),
            kind=kind,
            downloaded=False,
        )

    if not settings.allow_network:
        raise DownloadError(
            f"Source {key!r} ({dest.name}) is missing from {settings.raw_dir} and network access is "
            f"disabled (NLFED_ALLOW_NETWORK=false). Enable network access or place the file there "
            f"manually (URL: {url})."
        )

    own_client = client is None
    http = client or _client()
    try:
        with Timer(log, label):
            if kind == "odata":
                doc = fetch_odata(http, url, label)
                payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                sha, size = _write_bytes(dest, payload)
            elif kind == "wfs_geojson":
                doc = fetch_wfs_geojson(http, url, label, page_size=source.page_size)
                payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                sha, size = _write_bytes(dest, payload)
            else:
                sha, size = with_retries(lambda: _stream_to_file(http, url, dest, label), label)
    finally:
        if own_client:
            http.close()
    log.info("%s: %.1f MB sha256=%s", label, size / 1e6, sha[:12], extra=log_ctx(bytes=size))
    return DownloadRecord(
        key=key,
        name=source.name_for(year),
        publisher=source.publisher,
        url=url,
        license=source.license,
        path=_display_path(dest),
        sha256=sha,
        size_bytes=size,
        retrieved_at=_now_iso(),
        vintage=source.vintage_for(year),
        kind=kind,
        downloaded=True,
    )


def download_all(
    year: int | None = None,
    force: bool = False,
    keys: Iterable[str] | None = None,
    config: GeographyConfig | None = None,
) -> list[DownloadRecord]:
    """Download every configured source for geography ``year`` into ``data/raw``.

    Existing files are reused (hashed, not re-downloaded) unless ``force``.  Writes
    ``data/raw/sources_<year>.json``.  Raises :class:`DownloadError` when a file is missing and
    ``settings.allow_network`` is False, or when a download fails permanently.
    """
    settings = get_settings()
    cfg = config or load_geography_config()
    year = int(year or settings.geography_year)
    selected = list(keys) if keys is not None else list(cfg.sources)
    unknown = [k for k in selected if k not in cfg.sources]
    if unknown:
        raise DownloadError(f"Unknown source key(s): {', '.join(unknown)}")
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    records: list[DownloadRecord] = []
    client: httpx.Client | None = None
    try:
        with Timer(log, f"download_all({year})"):
            for key in selected:
                source = cfg.sources[key]
                needs_network = force or not raw_path(source, year).exists()
                if needs_network and settings.allow_network and client is None:
                    client = _client()
                records.append(download_source(key, source, year, force=force, client=client))
    finally:
        if client is not None:
            client.close()
    path = _write_sources_manifest(year, records)
    log.info(
        "sources recorded in %s (%d downloaded, %d reused)",
        path,
        sum(r.downloaded for r in records),
        sum(not r.downloaded for r in records),
    )
    return records
