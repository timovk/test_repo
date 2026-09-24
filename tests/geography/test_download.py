"""Downloads: offline behaviour, OData pagination, WFS paging, retries (no real network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.errors import DownloadError
from app.geography.config import SourceSpec, load_geography_config
from app.geography.download import (
    download_all,
    download_source,
    fetch_odata,
    fetch_wfs_geojson,
    read_sources_manifest,
    with_retries,
)


def test_missing_file_without_network_raises(tmp_data_dir: Path) -> None:
    with pytest.raises(DownloadError, match="network access is disabled"):
        download_all(2025)


def test_existing_files_are_reused(tmp_data_dir: Path) -> None:
    cfg = load_geography_config()
    for key, src in cfg.sources.items():
        (tmp_data_dir / "raw" / src.filename_for(2025)).write_bytes(f"payload-{key}".encode())
    records = download_all(2025)
    assert all(not r.downloaded for r in records)
    manifest = read_sources_manifest(2025)
    assert set(manifest) == set(cfg.sources)
    sup = manifest["supplement"]
    assert sup.vintage == 2022 and sup.size_bytes == len(b"payload-supplement")
    assert "{year}" not in manifest["wijkenbuurten"].url and "2025" in manifest["wijkenbuurten"].url
    # second call trusts the recorded hash (same size) and keeps the original retrieval time
    again = {r.key: r for r in download_all(2025)}
    assert again["supplement"].sha256 == sup.sha256
    assert again["supplement"].retrieved_at == sup.retrieved_at


def _odata_handler(request: httpx.Request) -> httpx.Response:
    skip = int(request.url.params.get("$skip", "0"))
    rows = [
        {"Codering_3": f"BU{i:08d}  ", "SoortRegio_2": "Buurt     "} for i in range(skip, min(skip + 3, 7))
    ]
    body: dict[str, object] = {"odata.metadata": "meta", "value": rows}
    if skip + 3 < 7:
        body["odata.nextLink"] = str(request.url.copy_merge_params({"$skip": str(skip + 3)}))
    return httpx.Response(200, json=body)


def test_odata_pagination_merges_pages() -> None:
    with httpx.Client(transport=httpx.MockTransport(_odata_handler)) as client:
        doc = fetch_odata(client, "https://example.test/ODataFeed/odata/X/TypedDataSet?$format=json", "t")
    assert doc["pages"] == 3
    assert [r["Codering_3"].strip() for r in doc["value"]] == [f"BU{i:08d}" for i in range(7)]


def test_wfs_paging() -> None:
    features = [
        {"type": "Feature", "properties": {"statcode": f"GM{i:04d}"}, "geometry": None} for i in range(5)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startIndex", "0"))
        count = int(request.url.params.get("count", "2"))
        page = features[start : start + min(count, 2)]
        return httpx.Response(200, json={"type": "FeatureCollection", "numberMatched": 5, "features": page})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        doc = fetch_wfs_geojson(
            client, "https://example.test/wfs?service=WFS&outputFormat=application/json", "t", 2
        )
    assert [f["properties"]["statcode"] for f in doc["features"]] == [f"GM{i:04d}" for i in range(5)]


def test_retries_with_backoff_then_success() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert with_retries(flaky, "t", attempts=5, base_delay=1.0, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]


def test_client_errors_are_not_retried() -> None:
    request = httpx.Request("GET", "https://example.test/x")
    sleeps: list[float] = []

    def not_found() -> None:
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    with pytest.raises(DownloadError):
        with_retries(not_found, "t", sleep=sleeps.append)
    assert sleeps == []


def test_streamed_file_download(tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NLFED_ALLOW_NETWORK", "true")
    from app.core.settings import reset_settings_cache

    reset_settings_cache()
    payload = b"x" * 3_000_000
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})
    )
    spec = SourceSpec(
        name="t {year}", publisher="p", url="https://example.test/f_{year}.bin", filename="f_{year}.bin"
    )
    with httpx.Client(transport=transport) as client:
        rec = download_source("t", spec, 2025, client=client)
    path = tmp_data_dir / "raw" / "f_2025.bin"
    assert rec.downloaded and rec.size_bytes == len(payload) and path.read_bytes() == payload
    assert rec.url == "https://example.test/f_2025.bin" and len(rec.sha256) == 64
    assert not list((tmp_data_dir / "raw").glob("*.part"))
    doc = json.dumps(rec.to_json())
    assert '"bytes": 3000000' in doc


# --------------------------------------------------------------------------- review regressions
def test_sources_manifest_concurrent_writers_and_no_needless_rewrite(tmp_data_dir: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.geography.download import sources_manifest_path

    cfg = load_geography_config()
    for key, src in cfg.sources.items():
        (tmp_data_dir / "raw" / src.filename_for(2025)).write_bytes(f"payload-{key}".encode())
    with ThreadPoolExecutor(max_workers=8) as pool:  # a fixed temp name made writers clobber each other
        results = list(pool.map(lambda _: download_all(2025), range(16)))
    assert all(len(r) == len(cfg.sources) for r in results)
    path = sources_manifest_path(2025)
    assert set(json.loads(path.read_text())["sources"]) == set(cfg.sources)
    assert not list((tmp_data_dir / "raw").glob("*.part"))
    before = path.stat().st_mtime_ns
    download_all(2025)  # nothing changed: the provenance file is not rewritten
    assert path.stat().st_mtime_ns == before
