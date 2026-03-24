from openai import OpenAI
from sqlalchemy import text

from core.db import engine
from pipeline.state import ChatState

# ── 클라이언트 ──────────────────────────────────────────────────────────────────
llm = OpenAI()
LLM_MODEL = "gpt-4o-mini"

# ── 임베딩 모델 (lazy loading) ──────────────────────────────────────────────────
_dense_model = None


class SearchPoint:
    def __init__(self, payload: dict, score: float):
        self.payload = payload
        self.score = score


COLLECTIONS = {
    "products": {
        "table": "products",
        "payload": """
            jsonb_build_object(
                'goods_id', goods_id,
                'product_name', product_name,
                'brand_name', brand_name,
                'prefix', prefix,
                'price', price,
                'discount_price', discount_price,
                'sold_out', sold_out,
                'soldout_reliable', soldout_reliable,
                'pet_type', pet_type,
                'category', category,
                'subcategory', subcategory,
                'health_concern_tags', health_concern_tags,
                'main_ingredients', main_ingredients,
                'ingredient_text_ocr', ingredient_text_ocr,
                'popularity_score', popularity_score,
                'sentiment_avg', sentiment_avg,
                'repeat_rate', repeat_rate,
                'thumbnail_url', thumbnail_url,
                'product_url', product_url
            )
        """,
    },
    "domain_qna": {
        "table": "domain_qna",
        "payload": """
            jsonb_build_object(
                'no', no,
                'species', species,
                'category', category,
                'source', source,
                'question', question,
                'answer', answer,
                'notes', notes
            )
        """,
    },
    "breed_meta": {
        "table": "breed_meta",
        "payload": """
            jsonb_build_object(
                'no', no,
                'pet_type', pet_type,
                'breed_name', breed_name,
                'breed_name_en', breed_name_en,
                'breed_group', breed_group,
                'size_class', size_class,
                'age_group', age_group,
                'care_difficulty', care_difficulty,
                'preferred_food', preferred_food,
                'health_products', health_products,
                'health_keywords', health_keywords
            )
        """,
    },
}


def get_models():
    global _dense_model
    if _dense_model is None:
        from fastembed import TextEmbedding

        print("Dense 모델 로드 중...")
        _dense_model = TextEmbedding("intfloat/multilingual-e5-large")
        print("모델 로드 완료")
    return _dense_model


def embed(query: str) -> list[float]:
    dense_model = get_models()
    return list(dense_model.embed([f"query: {query}"]))[0].tolist()


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _compile_filters(collection: str, filters: dict | None, exclude_filters: dict | None) -> tuple[str, dict]:
    filters = filters or {}
    exclude_filters = exclude_filters or {}
    clauses = ["1=1"]
    params: dict = {}

    if collection == "products":
        if "sold_out" in filters:
            clauses.append("sold_out = :sold_out")
            params["sold_out"] = bool(filters["sold_out"])

        for field in ("pet_type", "category", "subcategory", "health_concern_tags"):
            values = _as_list(filters.get(field))
            if values:
                param_name = f"{field}_values"
                clauses.append(f"{field} && CAST(:{param_name} AS TEXT[])")
                params[param_name] = values

        price_lte = filters.get("price_lte")
        if price_lte is not None:
            clauses.append("COALESCE(discount_price, price) <= :price_lte")
            params["price_lte"] = int(price_lte)

        prefixes = _as_list(filters.get("prefix"))
        if prefixes:
            clauses.append("prefix = ANY(CAST(:prefix_values AS TEXT[]))")
            params["prefix_values"] = prefixes

        excluded_ingredients = _as_list(exclude_filters.get("main_ingredients"))
        if excluded_ingredients:
            clauses.append("NOT (main_ingredients && CAST(:exclude_main_ingredients AS TEXT[]))")
            params["exclude_main_ingredients"] = excluded_ingredients

    elif collection == "domain_qna":
        species = _as_list(filters.get("species"))
        if species:
            clauses.append("species = ANY(CAST(:species_values AS TEXT[]))")
            params["species_values"] = species

        categories = _as_list(filters.get("category"))
        if categories:
            clauses.append("category = ANY(CAST(:category_values AS TEXT[]))")
            params["category_values"] = categories

    elif collection == "breed_meta":
        pet_types = _as_list(filters.get("pet_type"))
        if pet_types:
            clauses.append("pet_type = ANY(CAST(:pet_type_values AS TEXT[]))")
            params["pet_type_values"] = pet_types

        breed_names = _as_list(filters.get("breed_name"))
        if breed_names:
            clauses.append("breed_name = ANY(CAST(:breed_name_values AS TEXT[]))")
            params["breed_name_values"] = breed_names

        age_groups = _as_list(filters.get("age_group"))
        if age_groups:
            clauses.append("age_group = ANY(CAST(:age_group_values AS TEXT[]))")
            params["age_group_values"] = age_groups

    return " AND ".join(clauses), params


def hybrid_search(
    collection: str,
    query: str,
    top_k: int = 10,
    filters: dict | None = None,
    exclude_filters: dict | None = None,
):
    if collection not in COLLECTIONS:
        raise ValueError(f"지원하지 않는 collection: {collection}")

    config = COLLECTIONS[collection]
    where_clause, filter_params = _compile_filters(collection, filters, exclude_filters)
    query_vector = _vector_literal(embed(query))
    candidate_limit = max(top_k * 3, top_k)

    sql = f"""
    WITH dense_ranked AS (
        SELECT
            id,
            {config['payload']} AS payload,
            ROW_NUMBER() OVER (ORDER BY dense_embedding <=> CAST(:query_vector AS vector), id) AS rank_dense
        FROM {config['table']}
        WHERE {where_clause}
        ORDER BY dense_embedding <=> CAST(:query_vector AS vector), id
        LIMIT :candidate_limit
    ),
    sparse_ranked AS (
        SELECT
            id,
            {config['payload']} AS payload,
            ROW_NUMBER() OVER (
                ORDER BY ts_rank(text_search, plainto_tsquery('simple', :query_text)) DESC, id
            ) AS rank_sparse
        FROM {config['table']}
        WHERE {where_clause}
          AND text_search @@ plainto_tsquery('simple', :query_text)
        ORDER BY ts_rank(text_search, plainto_tsquery('simple', :query_text)) DESC, id
        LIMIT :candidate_limit
    )
    SELECT
        COALESCE(d.id, s.id) AS id,
        COALESCE(d.payload, s.payload) AS payload,
        COALESCE(1.0 / (60 + d.rank_dense), 0.0) +
        COALESCE(1.0 / (60 + s.rank_sparse), 0.0) AS score
    FROM dense_ranked d
    FULL OUTER JOIN sparse_ranked s USING (id)
    ORDER BY score DESC, id
    LIMIT :top_k
    """

    params = {
        "query_vector": query_vector,
        "query_text": query,
        "candidate_limit": candidate_limit,
        "top_k": top_k,
        **filter_params,
    }

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    return [SearchPoint(payload=dict(row["payload"]), score=float(row["score"])) for row in rows]


def fetch_rows(collection: str, limit: int = 20) -> list[dict]:
    if collection not in COLLECTIONS:
        raise ValueError(f"지원하지 않는 collection: {collection}")

    config = COLLECTIONS[collection]
    sql = f"""
    SELECT {config['payload']} AS payload
    FROM {config['table']}
    ORDER BY id DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"limit": limit}).mappings().all()
    return [dict(row["payload"]) for row in rows]


# ── 공통 헬퍼 ───────────────────────────────────────────────────────────────────

DOMAIN_INTENT_TO_CATEGORY = {
    "health_disease":      "건강 및 질병",
    "care_management":     "사육 및 관리",
    "nutrition_diet":      "영양 및 식단",
    "behavior_psychology": "행동 및 심리",
    "travel":              "여행 및 이동",
}


def build_pet_context(state: ChatState) -> str:
    p = state.get("pet_profile") or {}
    parts = []
    if p.get("species"):
        parts.append(f"종: {'강아지' if p['species'] == 'dog' else '고양이'}")
    if p.get("breed"):
        parts.append(f"품종: {p['breed']}")
    if p.get("age"):
        parts.append(f"나이: {p['age']}")
    if state.get("health_concerns"):
        parts.append(f"건강관심사: {', '.join(state['health_concerns'])}")
    if state.get("allergies"):
        parts.append(f"알레르기: {', '.join(state['allergies'])}")
    return " / ".join(parts) if parts else "펫 프로필 없음"
