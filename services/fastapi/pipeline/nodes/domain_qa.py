import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.utils import DOMAIN_INTENT_TO_CATEGORY, LLM_MODEL, build_pet_context, hybrid_search, llm
from pipeline.state import ChatState


def general_node(state: ChatState) -> dict:
    """쿼리 정제: 모호한 질문을 펫 프로필 기반으로 검색 최적화"""
    pet_ctx = build_pet_context(state)
    prompt = (
        "다음 질문을 반려동물 정보를 반영해 검색에 최적화된 한 문장으로 재작성하세요.\n"
        f"펫 정보: {pet_ctx}\n질문: {state['user_input']}"
    )
    refined = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    ).choices[0].message.content.strip()

    print(f"[GENERAL] 정제 쿼리: {refined}")
    return {"search_query": refined}


def rag_node(state: ChatState) -> dict:
    """domain_qna Hybrid Search (species + category 필터)"""
    query = state.get("search_query") or state["user_input"]
    domain_intent = state.get("domain_intent")
    species = (state.get("pet_profile") or {}).get("species")

    filters = {}
    if species:
        filters["species"] = [species, "both"]
    if domain_intent in DOMAIN_INTENT_TO_CATEGORY:
        filters["category"] = DOMAIN_INTENT_TO_CATEGORY[domain_intent]

    points = hybrid_search("domain_qna", query, top_k=5, filters=filters)
    contexts = [
        f"{point.payload.get('question', '')}\n{point.payload.get('answer', '')}".strip()
        for point in points
    ]
    print(f"[RAG] {len(contexts)}개 컨텍스트 (domain_intent={domain_intent})")
    return {"domain_contexts": contexts}
