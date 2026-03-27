# scripts/ 사용 가이드

## DB 관리 스크립트

로컬 개발 환경의 PostgreSQL(pgvector) DB를 관리하는 스크립트입니다.

### 사전 조건

- Docker Desktop 실행 중
- `infra/.env` 설정 완료 (`infra/.env.example` 복사 후 `POSTGRES_PASSWORD` 등 입력)

---

### setup_db.sh — 로컬 DB 셋업 / 복원

덤프 파일로부터 로컬 DB를 셋업합니다.
스키마는 Django migration이 만들고, dump는 앱 데이터만 복원합니다.

```bash
# 기본: backup/ 폴더에서 최신 .dump 파일 자동 선택
bash scripts/setup_db.sh

# 특정 덤프 파일 지정
bash scripts/setup_db.sh backup/tailtalk_db_20260324_150000.dump

# 데이터만 리셋 (스키마 유지, 테이블 truncate 후 데이터 복원)
bash scripts/setup_db.sh --data-only backup/tailtalk_db_20260324_150000.dump

# 덤프 내용 목록만 확인
bash scripts/setup_db.sh --list backup/tailtalk_db_20260324_150000.dump
```

**동작 순서 (기본 모드)**:
1. postgres 컨테이너 기동 + health check 대기
2. 기존 DB DROP → CREATE
3. `python manage.py migrate --noinput` 실행
4. pg_restore로 앱 데이터만 복원
5. 테이블 현황 출력

**복원 후 추가 명령은 필요 없습니다.**
`setup_db.sh`가 migration까지 자동으로 처리합니다.

---

### dump_db.sh — DB 덤프 생성

현재 로컬 DB를 덤프 파일로 저장합니다.

```bash
# 타임스탬프 파일명 자동 생성 (backup/tailtalk_db_20260324_153000.dump)
bash scripts/dump_db.sh

# 파일명 직접 지정
bash scripts/dump_db.sh backup/my_backup.dump
```

**참고**:
- dump에는 스키마가 포함되지 않고 앱 데이터만 포함됩니다
- 복원 시 `setup_db.sh`가 먼저 `migrate`를 실행해 스키마를 생성합니다
- 덤프 형식: PostgreSQL custom format (`-Fc`, 압축됨)
- 덤프 파일은 `.gitignore`에 포함하여 Git에 올리지 않습니다

---

## 팀원 온보딩 (처음 셋업)

```bash
# 1. 코드 pull
git pull origin develop

# 2. 환경변수 설정
cp infra/.env.example infra/.env
# infra/.env 에서 POSTGRES_PASSWORD 등 입력

# 3. DB 셋업 (backup/ 에 덤프 파일이 공유되어 있어야 함)
bash scripts/setup_db.sh backup/tailtalk_db_20260324_150000.dump
```

---

## 데이터 파이프라인 스크립트

Gold parquet 데이터를 PostgreSQL에 적재하는 스크립트입니다.
덤프 파일이 이미 있다면 위 `setup_db.sh`로 충분하며, 아래는 직접 적재할 때 사용합니다.

### ingest_postgres.py — 상품 + 리뷰 + 벡터 적재

```bash
# 전체 적재 (상품 → 벡터 → 리뷰)
POSTGRES_HOST=localhost python scripts/ingest_postgres.py

# 상품만 (벡터 포함)
POSTGRES_HOST=localhost python scripts/ingest_postgres.py --only goods

# 리뷰만
POSTGRES_HOST=localhost python scripts/ingest_postgres.py --only reviews

# 벡터/tsvector만 재생성 (상품 메타 스킵)
POSTGRES_HOST=localhost python scripts/ingest_postgres.py --only vectors

# 기존 데이터 삭제 후 재적재
POSTGRES_HOST=localhost python scripts/ingest_postgres.py --truncate
```

### domain/ingest_domain_postgres.py — 도메인 QnA + 품종 메타 적재

```bash
# 전체 (QnA + 품종 메타)
POSTGRES_HOST=localhost python scripts/domain/ingest_domain_postgres.py --table all

# 개별
POSTGRES_HOST=localhost python scripts/domain/ingest_domain_postgres.py --table qna
POSTGRES_HOST=localhost python scripts/domain/ingest_domain_postgres.py --table breed

# 기존 데이터 삭제 후 재적재
POSTGRES_HOST=localhost python scripts/domain/ingest_domain_postgres.py --table all --truncate
```

### domain/convert_domain_data.py — 도메인 Excel → Parquet 변환

```bash
# domain-data/*.xlsx → output/domain/*.parquet
python scripts/domain/convert_domain_data.py
```

---

## 디렉토리 구조

```
scripts/
├── setup_db.sh                     # 로컬 DB 셋업/복원
├── dump_db.sh                      # DB 덤프 생성
├── ingest_postgres.py              # 상품/리뷰/벡터 적재
├── config.py                       # ETL 공통 설정
├── domain/
│   ├── convert_domain_data.py      # Excel → Parquet 변환
│   └── ingest_domain_postgres.py   # 도메인 QnA/품종 적재
├── bronze/                         # Bronze ETL
├── silver/                         # Silver ETL
├── gold/                           # Gold ETL
└── eda/                            # 탐색적 데이터 분석
```
