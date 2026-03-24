import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.utils import LLM_MODEL, build_pet_context, hybrid_search, llm
from pipeline.state import ChatState


def _split_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,/|\n·]+", value)
    return [part.strip() for part in parts if part and part.strip()]


# ── profile_node ──────────────────────────────────────────────────────────────

def profile_node(state: ChatState) -> dict:
    """breed_meta 조회: 품종 기반 health_concern_tags 보완"""
    pet_profile = dict(state.get("pet_profile") or {})
    breed = pet_profile.get("breed")
    species = pet_profile.get("species")

    extra_concerns: list[str] = []

    if breed:
        filters = {"breed_name": breed}
        if species:
            filters["pet_type"] = species
        points = hybrid_search("breed_meta", breed, top_k=1, filters=filters)
        if points:
            payload = points[0].payload
            extra_concerns = list(payload.get("health_keywords") or [])
            if not extra_concerns:
                extra_concerns = (
                    _split_keywords(payload.get("health_products"))
                    + _split_keywords(payload.get("preferred_food"))
                )
            print(f"[PROFILE] breed={breed} → health_keywords={extra_concerns}")
        else:
            print(f"[PROFILE] breed_meta 미검색: {breed}")
    else:
        print("[PROFILE] 품종 정보 없음 — 스킵")

    existing = list(state.get("health_concerns") or [])
    merged = list(dict.fromkeys(existing + extra_concerns))
    return {"health_concerns": merged}


# ── query_node ────────────────────────────────────────────────────────────────

def query_node(state: ChatState) -> dict:
    """검색 쿼리 생성 + PostgreSQL 필터 빌드"""
    pet_ctx = build_pet_context(state)
    filters = dict(state.get("filters") or {})
    relaxation = state.get("filter_relaxation_count", 0)

    category_hint = filters.get("category") or ""
    subcategory_hint = filters.get("subcategory") or ""

    prompt = (
        "반려동물 상품 검색을 위한 최적화된 한국어 검색어를 한 문장으로만 반환하세요.\n"
        f"펫 정보: {pet_ctx}\n"
        f"카테고리: {category_hint} / 세부: {subcategory_hint}\n"
        f"원래 질문: {state['user_input']}"
    )
    search_query = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    ).choices[0].message.content.strip()

    search_filters = {"sold_out": False}
    exclude_filters = {}

    pet_type = filters.get("pet_type")
    if pet_type:
        search_filters["pet_type"] = pet_type

    if relaxation == 0:
        if category_hint:
            search_filters["category"] = category_hint
        if subcategory_hint:
            search_filters["subcategory"] = subcategory_hint
    else:
        if category_hint:
            search_filters["category"] = category_hint
        print(f"[QUERY] 필터 완화 (relaxation={relaxation}): subcategory 제거")

    budget = state.get("budget")
    if budget:
        search_filters["price_lte"] = budget

    allergies = state.get("allergies") or []
    if allergies:
        exclude_filters["main_ingredients"] = allergies

    print(f"[QUERY] query={search_query!r}, relaxation={relaxation}")
    return {
        "search_query": search_query,
        "filters": {**filters, "_search": search_filters, "_exclude": exclude_filters},
    }


# ── search_node ───────────────────────────────────────────────────────────────

def search_node(state: ChatState) -> dict:
    """products Hybrid Search via PostgreSQL + pgvector"""
    query = state.get("search_query") or state["user_input"]
    filters = dict(state.get("filters") or {})
    relaxation = state.get("filter_relaxation_count", 0)
    allergies = state.get("allergies") or []

    search_filters = dict(filters.get("_search") or {"sold_out": False})
    exclude_filters = dict(filters.get("_exclude") or {})

    if allergies and "main_ingredients" not in exclude_filters:
        exclude_filters["main_ingredients"] = allergies

    points = hybrid_search(
        "products",
        query,
        top_k=20,
        filters=search_filters,
        exclude_filters=exclude_filters,
    )
    candidates = [point.payload | {"_score": point.score} for point in points]

    if allergies:
        def safe(candidate):
            ocr = (candidate.get("ingredient_text_ocr") or "").lower()
            return not any(allergy.lower() in ocr for allergy in allergies)

        candidates = [candidate for candidate in candidates if safe(candidate)]

    print(f"[SEARCH] {len(candidates)}개 후보 (relaxation={relaxation})")
    return {"search_results": candidates}


# ── rerank_node ───────────────────────────────────────────────────────────────

_ALPHA = 0.50
_BETA = 0.25
_GAMMA = 0.15
_DELTA = 0.10
_EPSILON = 0.10

_TOP_K = 5


def _normalize(values: list[float]) -> list[float]:
    mn, mx = min(values), max(values)
    if mx == mn:
        return [1.0] * len(values)
    return [(value - mn) / (mx - mn) for value in values]


def rerank_node(state: ChatState) -> dict:
    """재랭킹: α·β·γ·δ·ε 가중치 + Fallback A/B/C/D"""
    candidates = state.get("search_results") or []
    detected_aspect = state.get("detected_aspect")
    relaxation = state.get("filter_relaxation_count", 0)

    if not candidates:
        print("[RERANK] 후보 없음")
        return {
            "reranked_results": [],
            "filter_relaxation_count": relaxation + 1 if relaxation < 1 else relaxation,
        }

    rrf_scores = [candidate.get("_score", 0.0) for candidate in candidates]
    pop_scores = [candidate.get("popularity_score") for candidate in candidates]
    sent_scores = [candidate.get("sentiment_avg") for candidate in candidates]
    rep_scores = [candidate.get("repeat_rate") for candidate in candidates]

    norm_rrf = _normalize(rrf_scores)
    norm_pop = _normalize([value if value is not None else 0.0 for value in pop_scores])

    scored = []
    for index, candidate in enumerate(candidates):
        has_sentiment = sent_scores[index] is not None
        has_repeat = rep_scores[index] is not None
        has_pop = pop_scores[index] is not None

        if not has_pop and not has_sentiment and not has_repeat:
            score = norm_rrf[index]
        elif not has_sentiment and not has_repeat:
            score = _ALPHA * norm_rrf[index] + 0.35 * norm_pop[index]
        else:
            gamma_v = sent_scores[index] if has_sentiment else 0.0
            delta_v = rep_scores[index] if has_repeat else 0.0
            score = (
                _ALPHA * norm_rrf[index]
                + _BETA * norm_pop[index]
                + _GAMMA * gamma_v
                + _DELTA * delta_v
            )

        if detected_aspect and candidate.get("sentiment_avg") is not None:
            score += _EPSILON * candidate["sentiment_avg"]

        scored.append((score, candidate))

    scored.sort(key=lambda item: item[0], reverse=True)
    top = [candidate for _, candidate in scored[:_TOP_K]]

    new_relaxation = relaxation
    if len(top) < 3 and relaxation < 1:
        new_relaxation = relaxation + 1
        print(f"[RERANK] 결과 부족 ({len(top)}개) → 필터 완화 예정 (relaxation → {new_relaxation})")
    else:
        print(f"[RERANK] 최종 {len(top)}개")

    return {
        "reranked_results": top,
        "filter_relaxation_count": new_relaxation,
    }
