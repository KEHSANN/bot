from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request

from app.catalog.schemas import CatalogBody, QuoteBody
from app.security.api import Limit, Offset, access_token, service
from app.security.metadata import request_metadata

router = APIRouter(prefix="/admin", tags=["Catalog and pricing"])
Kind = Literal["categories", "products", "plans"]


@router.get("/catalog/{kind}")
async def listing(
    request: Request, kind: Kind, limit: Limit = 50, offset: Offset = 0
) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request),
        request_metadata(request),
        "catalog.read",
        limit=limit,
        offset=offset,
        kind=kind,
    )


@router.get("/catalog/{kind}/{target}")
async def detail(request: Request, kind: Kind, target: UUID) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "catalog.read", target=target, kind=kind
    )


@router.post("/catalog/{kind}")
async def create(request: Request, kind: Kind, body: CatalogBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "catalog.create", body=body, kind=kind
    )


@router.put("/catalog/{kind}/{target}")
async def replace(request: Request, kind: Kind, target: UUID, body: CatalogBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request),
        request_metadata(request),
        "catalog.update",
        body=body,
        target=target,
        kind=kind,
    )


@router.post("/quotes")
async def quote(request: Request, body: QuoteBody) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "quotes.create", body=body
    )


@router.get("/quotes/{target}")
async def read_quote(request: Request, target: UUID) -> dict[str, Any]:
    return await service(request).admin_action(
        access_token(request), request_metadata(request), "quotes.read", target=target
    )
