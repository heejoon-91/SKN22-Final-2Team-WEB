# `bronze/goods -> silver/goods` 변경 작업 체크리스트

작성일: 2026-03-10

## 1) 변경 기준 확정 (선결정)

- [ ] **경로만 변경**인지, **레이어 의미까지 변경**인지 확정
  - 경로만 변경: 기존 Bronze 스키마(중복 포함)를 `silver/goods`에 그대로 저장
  - 레이어 의미까지 변경: `silver/goods`를 Silver 스키마(중복 제거 + 타입 변환)로만 저장
- [ ] 원시 상품 데이터 보존 필요 시 대체 경로 확정
  - 예: `bronze/goods_raw/` 또는 `bronze/goods_snapshot/`
- [ ] 파일명 규칙 확정
  - 현재: `output/silver/goods/YYYYMMDD_goods_silver.parquet`
  - 변경 시 기존 파일명과 호환 여부 결정

## 2) 코드 변경 작업

### A. 수집/가공 파이프라인

- [ ] `scripts/config.py`
  - `OUTPUT_DIR = "output/bronze"` 사용 영향 검토 후 필요 시 분리
  - 권장: `BRONZE_OUTPUT_DIR`, `SILVER_OUTPUT_DIR`로 분리해서 혼선 방지

- [ ] `scripts/bronze_goods.py`
  - 출력 경로(`output/bronze/goods/...`)를 변경 기준에 맞게 조정
  - 경로만 변경이 아니라면 스크립트명/로그 문구도 역할에 맞게 정리

- [ ] `scripts/bronze_detail_images.py`
  - `--input` 기본값: `output/bronze/goods/...` -> 신규 경로로 변경
  - help 문구(`Bronze goods parquet 경로`) 업데이트

- [ ] `scripts/silver_goods.py`
  - `--input` 기본값(`output/bronze/goods/...`) 변경
  - 만약 goods가 이미 Silver로 생성되면:
    - 스크립트 제거/통합 여부 결정
    - 중복 변환 방지 로직 추가(이미 Silver 스키마면 skip)

### B. 다운스트림 의존

- [ ] `scripts/bronze_reviews.py`
  - 기본값은 이미 `output/silver/goods/...`라 유지 가능
  - 단, 입력 컬럼(`goods_id`, `is_canonical`) 유지되는지 확인

- [ ] `scripts/eda/eda_goods.py`
  - `PARQUET = "output/bronze/goods/..."` 변경 필요
  - 분석 대상을 Bronze 기준으로 유지할지 Silver 기준으로 바꿀지 결정

- [ ] `scripts/eda/eda_reviews.py`
  - `SILVER_GOODS` 경로는 이미 Silver 기준
  - 파일명/날짜 패턴 변경 시 함께 수정

## 3) 문서 변경 작업

- [ ] `docs/data/03_medallion_schema.md`
  - `bronze/goods/`, `silver/goods/` 역할과 경로를 최종 설계와 일치시킴
  - Mermaid 다이어그램의 layer 흐름 수정

- [ ] `docs/data/02_data_collection_issues.md`
  - 예시 경로(`s3://bucket/bronze/goods/`) 수정

- [ ] `docs/data/04_goods_eda_issues.md`
  - 대상 파일 경로(`output/bronze/goods/...`) 수정

- [ ] 스크립트 상단 docstring 실행 예시 일괄 정리
  - `scripts/bronze_goods.py`
  - `scripts/bronze_detail_images.py`
  - `scripts/silver_goods.py`

## 4) 데이터 마이그레이션/운영 작업

- [ ] 기존 파일 이관 계획 수립
  - `output/bronze/goods/*.parquet` 이관(복사/변환) 여부 결정
  - 이전 산출물 참조 문서/리포트 재현성 유지 방식 결정

- [ ] 체크포인트 호환성 점검
  - `output/checkpoint_goods.json`
  - `output/checkpoint_detail_images.json`
  - 경로 변경 후 재사용 가능한지 확인

- [ ] 배치/스케줄 순서 재정의
  - 기존: `bronze_goods -> bronze_detail_images -> silver_goods -> bronze_reviews`
  - 변경 후 순서/의존관계 재정의

## 5) 검증 체크리스트

- [ ] `silver/goods` 산출 컬럼 검증
  - 최소: `goods_id`, `product_name`, `price`, `discount_price`, `rating`, `review_count`, `is_canonical`
- [ ] 건수 검증
  - 전체 row 수, unique `goods_id`, canonical 수 비교
- [ ] 리뷰 수집 영향 검증
  - `scripts/bronze_reviews.py` 실행 시 누락/오류 여부
- [ ] EDA 스크립트 재실행 검증
  - `scripts/eda/eda_goods.py`
  - `scripts/eda/eda_reviews.py`
- [ ] 문서/코드 경로 문자열 잔존 검증
  - `rg -n "output/bronze/goods|s3://bucket/bronze/goods|output/silver/goods" -S .`

## 6) 권장 적용 순서

1. 선결정(경로만 변경 vs 레이어 의미 변경) 확정
2. 코드 경로/입출력 변경
3. 문서 동기화
4. 과거 데이터 이관
5. 검증 체크리스트 수행

