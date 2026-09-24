"""REAL geography (provinces, municipalities), the FICTIONAL apportionment, and GeoJSON layers."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import geography as geo_read

router = APIRouter(tags=["geography"])

#: ``Cache-Control`` of GeoJSON layers (content-addressed by ETag, revalidated hourly).
GEO_CACHE_CONTROL = "public, max-age=3600, must-revalidate"


@router.get("/provinces", summary="The 12 provinces: REAL statistics, FICTIONAL seats / EV")
def provinces(session: SessionDep) -> ApiJSON:
    return respond(geo_read.provinces(session))


@router.get("/provinces/{code}", summary="Province detail: demographics, districts, Senate seats, governor")
def province(code: str, session: SessionDep) -> ApiJSON:
    return respond(geo_read.province_detail(session, code))


@router.get("/municipalities", summary="Municipalities with statistics and demographics")
def municipalities(session: SessionDep, province: str | None = None) -> ApiJSON:
    return respond(geo_read.municipalities(session, province))


@router.get(
    "/municipalities/{code}", summary="Municipality detail: districts, neighbourhoods, mayor, council"
)
def municipality(code: str, session: SessionDep) -> ApiJSON:
    return respond(geo_read.municipality_detail(session, code))


@router.get("/apportionment", summary="House seats and EV per province, priority list, method comparison")
def apportionment(session: SessionDep) -> ApiJSON:
    return respond(geo_read.apportionment(session))


@router.get(
    "/geo/{layer:path}",
    summary="GeoJSON layers: provinces, municipalities, districts, units/{PV}",
    response_class=FileResponse,
)
def geojson(layer: str, request: Request, session: SessionDep, plan: int | None = None) -> Response:
    """``/api/geo/provinces.geojson``, ``municipalities.geojson``, ``districts.geojson`` (active
    plan, or ``?plan=``) and ``units/NB.geojson`` — EPSG:4326, with ``ETag`` / ``Cache-Control``
    (``If-None-Match`` → 304)."""
    path = geo_read.geojson_layer(session, layer, plan_id=plan)
    st = path.stat()
    etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
    headers = {"ETag": etag, "Cache-Control": GEO_CACHE_CONTROL}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="application/geo+json", headers=headers)
