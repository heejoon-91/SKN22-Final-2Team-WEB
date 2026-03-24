from fastapi import APIRouter, Query

from pipeline.utils import hybrid_search

router = APIRouter()


@router.get("/")
async def recommend(
    query: str = Query(..., min_length=1),
    pet_type: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    limit: int = Query(5, ge=1, le=20),
):
    filters = {"sold_out": False}
    if pet_type:
        filters["pet_type"] = pet_type
    if category:
        filters["category"] = category
    if subcategory:
        filters["subcategory"] = subcategory

    results = hybrid_search("products", query, top_k=limit, filters=filters)
    return {"results": [result.payload | {"_score": result.score} for result in results]}
