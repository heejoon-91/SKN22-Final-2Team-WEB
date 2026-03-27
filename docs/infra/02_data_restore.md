# 데이터 복원 가이드

> DB 데이터 적재, 덤프, 복원 절차

---

## 1. 사전 준비

팀 공유 드라이브에서 아래 파일을 받아 레포 루트 `backup/` 디렉토리에 놓는다:

- `data_<날짜>.dump` — PostgreSQL 덤프 (상품 4,902개 / 리뷰 전체)
- `qdrant/products/<파일명>.snapshot` — Qdrant products 컬렉션 스냅샷
- `qdrant/domain_qna/<파일명>.snapshot` — Qdrant domain_qna 컬렉션 스냅샷 (2,411개)
- `qdrant/breed_meta/<파일명>.snapshot` — Qdrant breed_meta 컬렉션 스냅샷 (1,125개)

> `backup/` 디렉토리는 `.gitignore`에 포함되어 있어 커밋되지 않는다.

---

## 2. PostgreSQL 복원

> 스키마는 Django migration이 생성하고, dump는 앱 데이터만 복원한다.

```bash
# 1. 최신 덤프 자동 선택 복원
bash scripts/setup_db.sh

# 파일 직접 지정
bash scripts/setup_db.sh backup/data_20260319.dump
```

---

## 3. Qdrant 복원

컬렉션별로 동일한 방식으로 복원한다. `<COLLECTION>`과 `<SNAPSHOT_FILE>`을 교체해서 실행.

| 컬렉션 | 스냅샷 경로 | 예상 points_count |
|---|---|---|
| `products` | `backup/qdrant/products/<파일명>.snapshot` | 3,618 |
| `domain_qna` | `backup/qdrant/domain_qna/<파일명>.snapshot` | 2,411 |
| `breed_meta` | `backup/qdrant/breed_meta/<파일명>.snapshot` | 1,125 |

```bash
# 1. 전체 서비스 실행 (docker compose up -d 이미 실행했다면 생략)
cd infra && docker compose up -d

# 2. 전체 복원 (최신 스냅샷 자동 선택)
./scripts/restore_qdrant.sh

# 단일 컬렉션만 복원
./scripts/restore_qdrant.sh products
./scripts/restore_qdrant.sh domain_qna breed_meta
```

---

## 4. 덤프 파일 재생성 (데이터 담당자만)

Gold ETL 결과가 변경됐을 때만 재생성한다.

### PostgreSQL 덤프

```bash
bash scripts/dump_db.sh backup/data_$(date +%Y%m%d).dump
```

### Qdrant 스냅샷

3개 컬렉션 모두 재생성한다.

```bash
# 스냅샷 생성 및 로컬 저장
for col in products domain_qna breed_meta; do
  NAME=$(curl -s -X POST http://localhost:6333/collections/${col}/snapshots \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")
  mkdir -p backup/qdrant/${col}
  docker cp tailtalk-qdrant-1:/qdrant/snapshots/${col}/${NAME} backup/qdrant/${col}/
  docker cp tailtalk-qdrant-1:/qdrant/snapshots/${col}/${NAME}.checksum backup/qdrant/${col}/
  # 컨테이너 내 스냅샷 삭제
  curl -s -X DELETE http://localhost:6333/collections/${col}/snapshots/${NAME}
  echo "${col}: ${NAME}"
done
```

> 재생성 후 팀 공유 드라이브 파일 교체 및 팀 공지 필요.
