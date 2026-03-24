"""
output/domain/ Parquet → PostgreSQL domain_qna / breed_meta 검색 테이블 적재

- Dense: intfloat/multilingual-e5-large (1024d, fastembed)
- Sparse 대체: PostgreSQL text_search(tsvector)

실행:
  python scripts/domain/ingest_domain_qdrant.py --collection all
  python scripts/domain/ingest_domain_qdrant.py --collection qna
  python scripts/domain/ingest_domain_qdrant.py --collection breed
"""

import argparse
from datetime import datetime
from pathlib import Path
import re
import sys

import pandas as pd
from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR / "services" / "fastapi"))

from core.db import engine  # noqa: E402
from core.db_setup import setup_database  # noqa: E402

DOMAIN_DIR = BASE_DIR / "output" / "domain"
QNA_PARQUET = DOMAIN_DIR / "qna.parquet"
BREED_PARQUET = DOMAIN_DIR / "breed_meta.parquet"

UPSERT_QNA_SQL = text(
    """
    INSERT INTO domain_qna (
        no,
        species,
        category,
        source,
        question,
        answer,
        notes,
        dense_embedding,
        text_search
    ) VALUES (
        :no,
        :species,
        :category,
        :source,
        :question,
        :answer,
        :notes,
        CAST(:dense_embedding AS vector),
        to_tsvector('simple', :search_text)
    )
    ON CONFLICT (no) DO UPDATE SET
        species = EXCLUDED.species,
        category = EXCLUDED.category,
        source = EXCLUDED.source,
        question = EXCLUDED.question,
        answer = EXCLUDED.answer,
        notes = EXCLUDED.notes,
        dense_embedding = EXCLUDED.dense_embedding,
        text_search = EXCLUDED.text_search
    """
)

UPSERT_BREED_SQL = text(
    """
    INSERT INTO breed_meta (
        no,
        pet_type,
        breed_name,
        breed_name_en,
        breed_group,
        size_class,
        age_group,
        care_difficulty,
        preferred_food,
        health_products,
        health_keywords,
        dense_embedding,
        text_search
    ) VALUES (
        :no,
        :pet_type,
        :breed_name,
        :breed_name_en,
        :breed_group,
        :size_class,
        :age_group,
        :care_difficulty,
        :preferred_food,
        :health_products,
        :health_keywords,
        CAST(:dense_embedding AS vector),
        to_tsvector('simple', :search_text)
    )
    ON CONFLICT (no) DO UPDATE SET
        pet_type = EXCLUDED.pet_type,
        breed_name = EXCLUDED.breed_name,
        breed_name_en = EXCLUDED.breed_name_en,
        breed_group = EXCLUDED.breed_group,
        size_class = EXCLUDED.size_class,
        age_group = EXCLUDED.age_group,
        care_difficulty = EXCLUDED.care_difficulty,
        preferred_food = EXCLUDED.preferred_food,
        health_products = EXCLUDED.health_products,
        health_keywords = EXCLUDED.health_keywords,
        dense_embedding = EXCLUDED.dense_embedding,
        text_search = EXCLUDED.text_search
    """
)


def vector_literal(values) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def safe_str(value):
    if value is None or (isinstance(value, float) and value != value):
        return None
    cleaned = str(value).strip()
    return cleaned or None


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_list(value) -> list[str]:
    if value is None:
        return []
    try:
        return [item for item in list(value) if item]
    except TypeError:
        return []


def split_keywords(*values) -> list[str]:
    merged = []
    for value in values:
        if not value:
            continue
        parts = re.split(r"[,/|\n·]+", str(value))
        merged.extend(part.strip() for part in parts if part and part.strip())
    return list(dict.fromkeys(merged))


def build_qna_text(row) -> str:
    parts = [
        f"[카테고리] {row.get('category') or ''}",
        f"[질문] {row.get('question') or ''}",
        f"[답변] {row.get('answer') or ''}",
    ]
    notes = row.get("notes")
    if notes and str(notes).strip() and str(notes).strip().lower() != "nan":
        parts.append(f"[참고] {notes}")
    return "\n".join(parts).strip()


def build_breed_text(row) -> str:
    species_kr = "강아지" if row.get("species") == "dog" else "고양이"
    parts = [
        f"[품종] {row.get('breed_name') or ''} ({row.get('breed_name_en') or ''}) — {species_kr} / {row.get('group') or ''}",
        f"[연령대] {row.get('age_group') or ''}",
        f"[일반 특징] {row.get('general_traits') or ''}",
        f"[건강 특징] {row.get('health_traits') or ''}",
        f"[좋아하는 사료] {row.get('preferred_food') or ''}",
        f"[건강제품] {row.get('health_products') or ''}",
        f"[수의 영양학적 메타] {row.get('vet_nutrition_desc') or ''}",
    ]
    return "\n".join(part for part in parts if not part.endswith("— /") and not part.endswith("] ")).strip()


def run_qna(dense_model, batch_size: int) -> None:
    print("\n=== domain_qna 적재 ===")
    if not QNA_PARQUET.exists():
        raise FileNotFoundError(f"파일 없음: {QNA_PARQUET}  (convert_domain_data.py 먼저 실행)")

    df = pd.read_parquet(QNA_PARQUET)
    print(f"  로드: {QNA_PARQUET}  ({len(df):,}행)")
    texts = [build_qna_text(row) for _, row in df.iterrows()]
    rows = df.to_dict("records")

    from tqdm import tqdm

    with engine.begin() as conn:
        batch_count = max((len(rows) + batch_size - 1) // batch_size, 1)
        for start in tqdm(range(0, len(rows), batch_size), total=batch_count, unit="batch", desc="domain_qna 적재"):
            end = min(start + batch_size, len(rows))
            batch_rows = rows[start:end]
            batch_texts = texts[start:end]
            dense_vecs = list(dense_model.embed(batch_texts))
            payloads = []
            for row, search_text, dense_vector in zip(batch_rows, batch_texts, dense_vecs):
                payloads.append(
                    {
                        "no": int(row["no"]),
                        "species": row.get("species"),
                        "category": row.get("category"),
                        "source": row.get("source"),
                        "question": safe_str(row.get("question")),
                        "answer": safe_str(row.get("answer")),
                        "notes": safe_str(row.get("notes")),
                        "dense_embedding": vector_literal(dense_vector.tolist()),
                        "search_text": search_text,
                    }
                )
            conn.execute(UPSERT_QNA_SQL, payloads)


def run_breed(dense_model, batch_size: int) -> None:
    print("\n=== breed_meta 적재 ===")
    if not BREED_PARQUET.exists():
        raise FileNotFoundError(f"파일 없음: {BREED_PARQUET}  (convert_domain_data.py 먼저 실행)")

    df = pd.read_parquet(BREED_PARQUET)
    print(f"  로드: {BREED_PARQUET}  ({len(df):,}행)")
    texts = [build_breed_text(row) for _, row in df.iterrows()]
    rows = df.to_dict("records")

    from tqdm import tqdm

    with engine.begin() as conn:
        batch_count = max((len(rows) + batch_size - 1) // batch_size, 1)
        for start in tqdm(range(0, len(rows), batch_size), total=batch_count, unit="batch", desc="breed_meta 적재"):
            end = min(start + batch_size, len(rows))
            batch_rows = rows[start:end]
            batch_texts = texts[start:end]
            dense_vecs = list(dense_model.embed(batch_texts))
            payloads = []
            for row, search_text, dense_vector in zip(batch_rows, batch_texts, dense_vecs):
                payloads.append(
                    {
                        "no": int(row["no"]),
                        "pet_type": row.get("species"),
                        "breed_name": safe_str(row.get("breed_name")),
                        "breed_name_en": safe_str(row.get("breed_name_en")),
                        "breed_group": safe_str(row.get("group")),
                        "size_class": to_list(row.get("size_class")),
                        "age_group": safe_str(row.get("age_group")),
                        "care_difficulty": safe_int(row.get("care_difficulty")),
                        "preferred_food": safe_str(row.get("preferred_food")),
                        "health_products": safe_str(row.get("health_products")),
                        "health_keywords": split_keywords(row.get("health_traits"), row.get("health_products")),
                        "dense_embedding": vector_literal(dense_vector.tolist()),
                        "search_text": search_text,
                    }
                )
            conn.execute(UPSERT_BREED_SQL, payloads)


def main(collection: str, recreate: bool, batch_size: int) -> None:
    print(f"[ingest_domain_pgvector] 시작 — {datetime.now().strftime('%H:%M:%S')}")
    setup_database(recreate=False)

    print("\n  모델 로드 중 (fastembed)...")
    from fastembed import TextEmbedding

    dense_model = TextEmbedding("intfloat/multilingual-e5-large")
    print("  모델 로드 완료")

    if recreate:
        with engine.begin() as conn:
            if collection in ("qna", "all"):
                conn.execute(text("TRUNCATE TABLE domain_qna RESTART IDENTITY"))
                print("  [domain_qna] 기존 검색 데이터 삭제")
            if collection in ("breed", "all"):
                conn.execute(text("TRUNCATE TABLE breed_meta RESTART IDENTITY"))
                print("  [breed_meta] 기존 검색 데이터 삭제")

    if collection in ("qna", "all"):
        run_qna(dense_model, batch_size)
    if collection in ("breed", "all"):
        run_breed(dense_model, batch_size)

    print(f"\n[완료] {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="domain Parquet → PostgreSQL 검색 테이블 적재")
    parser.add_argument(
        "--collection",
        choices=["qna", "breed", "all"],
        default="all",
        help="적재할 컬렉션 (기본: all)",
    )
    parser.add_argument("--recreate", action="store_true", help="검색 테이블 재생성 후 적재")
    parser.add_argument("--batch", type=int, default=64, metavar="N", help="배치 크기 (기본 64)")
    args = parser.parse_args()
    main(collection=args.collection, recreate=args.recreate, batch_size=args.batch)
