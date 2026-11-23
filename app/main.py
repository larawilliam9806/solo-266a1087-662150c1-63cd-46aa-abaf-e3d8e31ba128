"""FastAPI application: package supply for after-sales replacement details."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import packing, service
from .db import connect, database_path, init_db
from .models import Health, Manifest, ManifestList, TicketLimitError

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


def _open() -> sqlite3.Connection:
    return connect(database_path())


def get_db() -> Iterator[sqlite3.Connection]:
    """One SQLite connection per request (WAL: readers don't block writers)."""
    conn = _open()
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = _open()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="售后换件明细分包供数",
    version="1.0.0",
    description=(
        "按批次接收工单表与换件表，按工单号左连接后依工单号字典序分包，"
        "发布不可变清单版本，并按版本号与包号逐包提供 CSV 下载。"
    ),
    lifespan=lifespan,
)

app.add_exception_handler(
    service.NotFound,
    lambda request, exc: JSONResponse(status_code=404, content={"detail": str(exc)}),
)
app.add_exception_handler(
    service.BadRequest,
    lambda request, exc: JSONResponse(status_code=400, content={"detail": str(exc)}),
)
app.add_exception_handler(
    packing.InputError,
    lambda request, exc: JSONResponse(status_code=422, content={"detail": str(exc)}),
)
app.add_exception_handler(
    service.CorruptSnapshot,
    lambda request, exc: JSONResponse(status_code=409, content={"detail": str(exc)}),
)


@app.exception_handler(packing.TicketTooLarge)
async def handle_ticket_too_large(
    request: Request, exc: packing.TicketTooLarge
) -> JSONResponse:
    payload = TicketLimitError.model_validate(
        {
            "detail": {
                "ticket_no": exc.ticket_no,
                "row_count": exc.row_count,
                "row_limit": exc.row_limit,
                "message": str(exc),
            }
        }
    )
    return JSONResponse(status_code=422, content=payload.model_dump())


@app.get("/health", response_model=Health, tags=["ops"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> Health:
    conn.execute("SELECT 1").fetchone()
    return Health(status="ok", database=str(database_path()))


@app.post(
    "/api/batches/{batch_id}/publish",
    response_model=Manifest,
    status_code=201,
    responses={
        422: {
            "model": TicketLimitError,
            "description": "某工单连接行数超过每包上限，本次不发布",
        }
    },
    tags=["publish"],
)
def publish_batch(
    batch_id: str,
    tickets: UploadFile = File(..., description="工单 CSV：ticket_no,station"),
    parts: UploadFile = File(..., description="换件 CSV：ticket_no,part_code"),
    row_limit: int = Form(..., gt=0, description="每包行数上限"),
    conn: sqlite3.Connection = Depends(get_db),
) -> Manifest:
    """接收同批两表与每包行数上限，校验后发布一个新清单版本。

    两表按工单号左连接，重复行各自保留，无换件的工单行也保留。任一工单
    连接后的行数超过上限时返回 422（含工单号与连接行数），本批不写库，
    原清单继续有效。
    """
    batch = service.require_batch_id(batch_id)
    limit = service.parse_row_limit(row_limit)
    manifest = service.publish(
        conn, batch, tickets.file.read(), parts.file.read(), limit
    )
    return Manifest.model_validate(service.with_download_urls(manifest))


@app.get(
    "/api/batches/{batch_id}/manifests",
    response_model=ManifestList,
    tags=["manifest"],
)
def list_batch_manifests(
    batch_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ManifestList:
    """列出版本号与各包首末工单号、行数；不展开连接明细。"""
    batch = service.require_batch_id(batch_id)
    manifests = service.list_manifests(conn, batch)
    return ManifestList(
        batch_id=batch,
        manifests=[
            Manifest.model_validate(service.with_download_urls(m)) for m in manifests
        ],
    )


@app.get(
    "/api/batches/{batch_id}/manifests/latest",
    response_model=Manifest,
    tags=["manifest"],
)
def latest_batch_manifest(
    batch_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Manifest:
    batch = service.require_batch_id(batch_id)
    manifest = service.latest_manifest(conn, batch)
    return Manifest.model_validate(service.with_download_urls(manifest))


@app.get("/api/manifests/{version}", response_model=Manifest, tags=["manifest"])
def get_manifest(
    version: str, conn: sqlite3.Connection = Depends(get_db)
) -> Manifest:
    """按版本号查看清单；发布新版后旧清单仍可查询。"""
    manifest = service.get_manifest(conn, version)
    return Manifest.model_validate(service.with_download_urls(manifest))


@app.get(
    "/api/manifests/{version}/manifest.csv",
    response_class=PlainTextResponse,
    responses={200: {"content": {CSV_MEDIA_TYPE: {}}}},
    tags=["manifest"],
)
def download_manifest_csv(
    version: str, conn: sqlite3.Connection = Depends(get_db)
) -> PlainTextResponse:
    """下载清单 CSV：版本号、各包首末工单号与行数（不展开明细）。"""
    manifest = service.get_manifest(conn, version)
    return PlainTextResponse(
        service.manifest_csv(manifest),
        media_type=CSV_MEDIA_TYPE,
        headers=_csv_headers(f"{version}-manifest.csv"),
    )


@app.get(
    "/api/manifests/{version}/packages/{package_no}.csv",
    responses={200: {"content": {CSV_MEDIA_TYPE: {}}}},
    tags=["download"],
)
def download_package(version: str, package_no: int) -> StreamingResponse:
    """按版本号与包号逐行下载本包 CSV，只读本包的源记录。"""
    conn = _open()
    try:
        # Resolve metadata first so an unknown version/package is a 404
        # instead of an error halfway through the stream.
        service.get_package_metadata(conn, version, package_no)
    except BaseException:
        conn.close()
        raise
    return StreamingResponse(
        _stream_package(conn, version, package_no),
        media_type=CSV_MEDIA_TYPE,
        headers=_csv_headers(f"{version}-p{package_no}.csv"),
    )


def _stream_package(
    conn: sqlite3.Connection, version: str, package_no: int
) -> Iterator[bytes]:
    try:
        for chunk in service.iter_package_csv(conn, version, package_no):
            yield chunk.encode("utf-8")
    finally:
        conn.close()


def _csv_headers(filename: str) -> dict:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }
