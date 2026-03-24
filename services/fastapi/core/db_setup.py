"""
PostgreSQL + pgvector 검색 테이블 초기화 스크립트

테이블:
  - products   : 상품 Hybrid Search (Dense + PostgreSQL FTS + RRF)
  - domain_qna : 반려동물 도메인 QnA RAG
  - breed_meta : 품종별 메타데이터 검색

실행:
  python -m core.db_setup
  python -m core.db_setup --recreate
"""

import argparse

from sqlalchemy import text

from core.db import engine


DDL = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """
    CREATE TABLE IF NOT EXISTS products (
        id BIGSERIAL PRIMARY KEY,
        goods_id TEXT NOT NULL UNIQUE,
        product_name TEXT,
        brand_name TEXT,
        prefix TEXT,
        price INTEGER,
        discount_price INTEGER,
        sold_out BOOLEAN DEFAULT FALSE,
        soldout_reliable BOOLEAN DEFAULT TRUE,
        pet_type TEXT[] DEFAULT '{}'::text[],
        category TEXT[] DEFAULT '{}'::text[],
        subcategory TEXT[] DEFAULT '{}'::text[],
        health_concern_tags TEXT[] DEFAULT '{}'::text[],
        main_ingredients TEXT[] DEFAULT '{}'::text[],
        ingredient_text_ocr TEXT,
        popularity_score DOUBLE PRECISION,
        sentiment_avg DOUBLE PRECISION,
        repeat_rate DOUBLE PRECISION,
        thumbnail_url TEXT,
        product_url TEXT,
        dense_embedding VECTOR(1024),
        text_search TSVECTOR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS domain_qna (
        id BIGSERIAL PRIMARY KEY,
        no INTEGER NOT NULL UNIQUE,
        species TEXT,
        category TEXT,
        source TEXT,
        question TEXT,
        answer TEXT,
        notes TEXT,
        dense_embedding VECTOR(1024),
        text_search TSVECTOR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS breed_meta (
        id BIGSERIAL PRIMARY KEY,
        no INTEGER NOT NULL UNIQUE,
        pet_type TEXT,
        breed_name TEXT,
        breed_name_en TEXT,
        breed_group TEXT,
        size_class TEXT[] DEFAULT '{}'::text[],
        age_group TEXT,
        care_difficulty INTEGER,
        preferred_food TEXT,
        health_products TEXT,
        health_keywords TEXT[] DEFAULT '{}'::text[],
        dense_embedding VECTOR(1024),
        text_search TSVECTOR
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_dense ON products USING hnsw (dense_embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_products_tsv ON products USING gin (text_search)",
    "CREATE INDEX IF NOT EXISTS idx_products_pet_type ON products USING gin (pet_type)",
    "CREATE INDEX IF NOT EXISTS idx_products_category ON products USING gin (category)",
    "CREATE INDEX IF NOT EXISTS idx_products_subcategory ON products USING gin (subcategory)",
    "CREATE INDEX IF NOT EXISTS idx_products_main_ingredients ON products USING gin (main_ingredients)",
    "CREATE INDEX IF NOT EXISTS idx_domain_qna_dense ON domain_qna USING hnsw (dense_embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_domain_qna_tsv ON domain_qna USING gin (text_search)",
    "CREATE INDEX IF NOT EXISTS idx_domain_qna_species ON domain_qna (species)",
    "CREATE INDEX IF NOT EXISTS idx_domain_qna_category ON domain_qna (category)",
    "CREATE INDEX IF NOT EXISTS idx_breed_meta_dense ON breed_meta USING hnsw (dense_embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_breed_meta_tsv ON breed_meta USING gin (text_search)",
    "CREATE INDEX IF NOT EXISTS idx_breed_meta_pet_type ON breed_meta (pet_type)",
    "CREATE INDEX IF NOT EXISTS idx_breed_meta_breed_name ON breed_meta (breed_name)",
]

DROP_ORDER = [
    "breed_meta",
    "domain_qna",
    "products",
]


def setup_database(recreate: bool = False) -> None:
    with engine.begin() as conn:
        if recreate:
            for table in DROP_ORDER:
                conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                print(f"  [{table}] 삭제 완료")

        for statement in DDL:
            conn.execute(text(statement))

    print("PostgreSQL + pgvector 검색 테이블 준비 완료")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pgvector 검색 테이블 초기화")
    parser.add_argument("--recreate", action="store_true", help="기존 검색 테이블 삭제 후 재생성")
    args = parser.parse_args()
    setup_database(recreate=args.recreate)
