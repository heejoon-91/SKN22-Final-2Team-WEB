"""
Gold goods parquet → PostgreSQL products 검색 테이블 적재

- GP 상품 (prefix=GP) 제외
- Dense: intfloat/multilingual-e5-large (1024d, fastembed)
- Sparse 대체: PostgreSQL text_search(tsvector)

실행:
  docker compose -f infra/docker-compose.yml run --rm \
      -v $(pwd)/output:/app/output \
      -v $(pwd)/scripts:/app/scripts \
      fastapi python scripts/ingest_qdrant.py

  # 옵션
  ... --recreate    # 검색 테이블 재생성 후 적재
  ... --batch 64    # 배치 크기 (기본 64)
"""

import argparse
from datetime import datetime
from glob import glob
from pathlib import Path
import sys

import pandas as pd
from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "services" / "fastapi"))

from core.db import engine  # noqa: E402
from core.db_setup import setup_database  # noqa: E402

GOODS_GLOB = "output/gold/goods/*_goods_gold.parquet"

UPSERT_SQL = text(
    """
    INSERT INTO products (
        goods_id,
        product_name,
        brand_name,
        prefix,
        price,
        discount_price,
        sold_out,
        soldout_reliable,
        pet_type,
        category,
        subcategory,
        health_concern_tags,
        main_ingredients,
        ingredient_text_ocr,
        popularity_score,
        sentiment_avg,
        repeat_rate,
        thumbnail_url,
        product_url,
        dense_embedding,
        text_search
    ) VALUES (
        :goods_id,
        :product_name,
        :brand_name,
        :prefix,
        :price,
        :discount_price,
        :sold_out,
        :soldout_reliable,
        :pet_type,
        :category,
        :subcategory,
        :health_concern_tags,
        :main_ingredients,
        :ingredient_text_ocr,
        :popularity_score,
        :sentiment_avg,
        :repeat_rate,
        :thumbnail_url,
        :product_url,
        CAST(:dense_embedding AS vector),
        to_tsvector('simple', :search_text)
    )
    ON CONFLICT (goods_id) DO UPDATE SET
        product_name = EXCLUDED.product_name,
        brand_name = EXCLUDED.brand_name,
        prefix = EXCLUDED.prefix,
        price = EXCLUDED.price,
        discount_price = EXCLUDED.discount_price,
        sold_out = EXCLUDED.sold_out,
        soldout_reliable = EXCLUDED.soldout_reliable,
        pet_type = EXCLUDED.pet_type,
        category = EXCLUDED.category,
        subcategory = EXCLUDED.subcategory,
        health_concern_tags = EXCLUDED.health_concern_tags,
        main_ingredients = EXCLUDED.main_ingredients,
        ingredient_text_ocr = EXCLUDED.ingredient_text_ocr,
        popularity_score = EXCLUDED.popularity_score,
        sentiment_avg = EXCLUDED.sentiment_avg,
        repeat_rate = EXCLUDED.repeat_rate,
        thumbnail_url = EXCLUDED.thumbnail_url,
        product_url = EXCLUDED.product_url,
        dense_embedding = EXCLUDED.dense_embedding,
        text_search = EXCLUDED.text_search
    """
)


def serialize_dict(value) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    return " ".join(f"{key} {item}" for key, item in value.items())


def to_list(value) -> list:
    if value is None:
        return []
    try:
        items = list(value)
        return [item for item in items if item is not None]
    except TypeError:
        return []


def safe_float(value):
    try:
        converted = float(value)
        return None if converted != converted else converted
    except (TypeError, ValueError):
        return None


def vector_literal(values) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def build_product_text(row) -> str:
    parts = [
        str(row.get("product_name") or ""),
        str(row.get("brand_name") or ""),
        " ".join(to_list(row.get("subcategory_names"))),
        " ".join(to_list(row.get("health_concern_tags"))),
        " ".join(to_list(row.get("main_ingredients"))),
        serialize_dict(row.get("ingredient_composition")),
        serialize_dict(row.get("nutrition_info")),
    ]
    return " ".join(part for part in parts if part).strip()


def latest_goods_path() -> str:
    files = sorted(glob(GOODS_GLOB))
    if not files:
        raise FileNotFoundError(f"파일 없음: {GOODS_GLOB}")
    return files[-1]


def main(recreate: bool, batch_size: int) -> None:
    print(f"[ingest_products_pgvector] 시작 — {datetime.now().strftime('%H:%M:%S')}")
    setup_database(recreate=False)

    path = latest_goods_path()
    print(f"  로드: {path}")
    df = pd.read_parquet(path)
    print(f"  전체: {len(df):,}행")

    df = df[df["prefix"] != "GP"].copy()
    print(f"  GP 제외 후: {len(df):,}행")

    print("  모델 로드 중 (fastembed)...")
    from fastembed import TextEmbedding

    dense_model = TextEmbedding("intfloat/multilingual-e5-large")
    print("  모델 로드 완료")

    texts = [build_product_text(row) for _, row in df.iterrows()]
    rows = df.to_dict("records")
    total = len(rows)

    from tqdm import tqdm

    with engine.begin() as conn:
        if recreate:
            conn.execute(text("TRUNCATE TABLE products RESTART IDENTITY"))
            print("  [products] 기존 검색 데이터 삭제")

        batch_count = max((total + batch_size - 1) // batch_size, 1)
        for start in tqdm(range(0, total, batch_size), total=batch_count, unit="batch", desc="products 적재"):
            end = min(start + batch_size, total)
            batch_rows = rows[start:end]
            batch_texts = texts[start:end]
            dense_vecs = list(dense_model.embed(batch_texts))

            payloads = []
            for row, search_text, dense_vector in zip(batch_rows, batch_texts, dense_vecs):
                payloads.append(
                    {
                        "goods_id": row["goods_id"],
                        "product_name": row.get("product_name"),
                        "brand_name": row.get("brand_name"),
                        "prefix": row.get("prefix"),
                        "price": int(row["price"]) if pd.notna(row.get("price")) else None,
                        "discount_price": int(row["discount_price"]) if pd.notna(row.get("discount_price")) else None,
                        "sold_out": bool(row["sold_out"]) if pd.notna(row.get("sold_out")) else False,
                        "soldout_reliable": bool(row["soldout_reliable"]) if pd.notna(row.get("soldout_reliable")) else True,
                        "pet_type": to_list(row.get("pet_type")),
                        "category": to_list(row.get("category")),
                        "subcategory": to_list(row.get("subcategory")),
                        "health_concern_tags": to_list(row.get("health_concern_tags")),
                        "main_ingredients": to_list(row.get("main_ingredients")),
                        "ingredient_text_ocr": row.get("ingredient_text_ocr") if pd.notna(row.get("ingredient_text_ocr")) else None,
                        "popularity_score": safe_float(row.get("popularity_score")),
                        "sentiment_avg": safe_float(row.get("sentiment_avg")),
                        "repeat_rate": safe_float(row.get("repeat_rate")),
                        "thumbnail_url": row.get("thumbnail_url"),
                        "product_url": row.get("product_url"),
                        "dense_embedding": vector_literal(dense_vector.tolist()),
                        "search_text": search_text,
                    }
                )
            conn.execute(UPSERT_SQL, payloads)

    print(f"적재 완료: {total:,}개 — {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gold goods → PostgreSQL products 검색 테이블 적재")
    parser.add_argument("--recreate", action="store_true", help="검색 테이블 재생성 후 적재")
    parser.add_argument("--batch", type=int, default=64, metavar="N", help="배치 크기 (기본 64)")
    args = parser.parse_args()
    main(recreate=args.recreate, batch_size=args.batch)
