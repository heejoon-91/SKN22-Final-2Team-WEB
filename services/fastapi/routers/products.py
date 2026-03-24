from fastapi import APIRouter, Query

from pipeline.utils import fetch_rows

router = APIRouter()


@router.get("/")
async def list_products(limit: int = Query(20, ge=1, le=100)):
    return {"items": fetch_rows("products", limit=limit)}
