"""Request/response schemas."""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class PackageSummary(BaseModel):
    package_no: int = Field(description="包号，从 1 开始按装包顺序递增")
    first_ticket_no: str = Field(description="本包首工单号")
    last_ticket_no: str = Field(description="本包末工单号")
    row_count: int = Field(description="本包连接明细行数")
    download_url: str = Field(description="逐行下载本包 CSV 的相对地址")


class Manifest(BaseModel):
    version: str = Field(description="清单版本号")
    batch_id: str = Field(description="批次号")
    row_limit: int = Field(description="本批每包行数上限")
    total_rows: int = Field(description="本批连接明细总行数")
    ticket_count: int = Field(description="本批去重后的工单号个数")
    package_count: int = Field(description="包数")
    created_at: str = Field(description="发布时刻（UTC）")
    packages: List[PackageSummary] = Field(description="各包首末工单号和行数")


class ManifestList(BaseModel):
    batch_id: str
    manifests: List[Manifest]


class TicketLimitExceeded(BaseModel):
    error: str = Field(default="ticket_row_limit_exceeded")
    ticket_no: str = Field(description="超出上限的工单号")
    row_count: int = Field(description="该工单连接后的明细行数")
    row_limit: int = Field(description="本批每包行数上限")
    message: str


class TicketLimitError(BaseModel):
    detail: TicketLimitExceeded


class Health(BaseModel):
    status: str
    database: str
