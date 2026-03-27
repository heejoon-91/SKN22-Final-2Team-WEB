#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# 로컬 DB 셋업 / 복원 스크립트
# 스키마는 Django migrations로 생성하고, dump는 앱 데이터만 복원한다.
#
# 사전 조건:
#   - docker compose 실행 가능
#   - infra/.env 설정 완료 (POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD)
#
# 사용법:
#   bash scripts/setup_db.sh                            # backup/ 에서 최신 .dump 자동 선택
#   bash scripts/setup_db.sh backup/my_backup.dump      # 특정 덤프 파일 지정
#   bash scripts/setup_db.sh --data-only backup/x.dump  # 데이터만 복원 (스키마 유지)
#   bash scripts/setup_db.sh --list backup/x.dump       # 덤프 내용 목록만 출력
#
# 옵션:
#   --data-only   스키마 유지, 데이터만 복원 (DB DROP 하지 않음)
#   --list        덤프 파일 내용 목록 출력 후 종료
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INFRA_DIR="$PROJECT_ROOT/infra"
COMPOSE_FILE="$INFRA_DIR/docker-compose.yml"
source "$SCRIPT_DIR/lib/db_tables.sh"

# ── 인자 파싱 ──
DATA_ONLY=false
LIST_ONLY=false
DUMP_FILE=""
TOTAL_STEPS=4

for arg in "$@"; do
    case $arg in
        --data-only) DATA_ONLY=true ;;
        --list)      LIST_ONLY=true ;;
        *)           DUMP_FILE="$arg" ;;
    esac
done

if [ "$DATA_ONLY" = true ]; then
    TOTAL_STEPS=5
fi

# 덤프 파일 결정: 인자 없으면 backup/ 디렉토리에서 최신 파일 자동 선택
if [ -z "$DUMP_FILE" ]; then
    DUMP_FILE=$(ls -t "$PROJECT_ROOT"/backup/*.dump 2>/dev/null | head -1 || true)
fi

if [ -z "$DUMP_FILE" ] || [ ! -f "$DUMP_FILE" ]; then
    echo "ERROR: 덤프 파일이 없습니다."
    echo "  사용법: bash scripts/setup_db.sh [dump_file.dump]"
    echo "  기본 경로: backup/*.dump"
    exit 1
fi

DUMP_FILE="$(cd "$(dirname "$DUMP_FILE")" && pwd)/$(basename "$DUMP_FILE")"
echo "dump: $(basename "$DUMP_FILE") ($(du -h "$DUMP_FILE" | cut -f1))"

# ── .env 로드 ──
if [ -f "$INFRA_DIR/.env" ]; then
    set -a
    source "$INFRA_DIR/.env"
    set +a
else
    echo "ERROR: infra/.env 파일이 없습니다. infra/.env.example을 복사하여 설정하세요."
    exit 1
fi

DB_NAME="${POSTGRES_DB:-tailtalk_db}"
DB_USER="${POSTGRES_USER:-mungnyang}"
DB_PASS="${POSTGRES_PASSWORD}"
APP_DATA_TABLES_SQL=$(printf "'%s'," "${APP_DATA_TABLES[@]}")
APP_DATA_TABLES_SQL="${APP_DATA_TABLES_SQL%,}"
RESTORE_TABLE_ARGS=()

for table in "${APP_DATA_TABLES[@]}"; do
    RESTORE_TABLE_ARGS+=("-t" "$table")
done

if [ -z "$DB_PASS" ]; then
    echo "ERROR: POSTGRES_PASSWORD가 설정되지 않았습니다."
    exit 1
fi

# ── 헬퍼: docker compose exec 축약 ──
dexec() {
    docker compose -f "$COMPOSE_FILE" exec -T postgres "$@"
}

drun_django() {
    docker compose -f "$COMPOSE_FILE" run --rm django "$@"
}

db_exists() {
    [ "$(dexec psql -U "$DB_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" = "1" ]
}

create_db() {
    dexec psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"
}

# ── postgres 컨테이너 기동 ──
echo ""
echo "[1/$TOTAL_STEPS] postgres 컨테이너 기동..."
docker compose -f "$COMPOSE_FILE" up -d postgres
until dexec pg_isready -U "$DB_USER" -d postgres > /dev/null 2>&1; do
    sleep 1
done
echo "  ready"

# ── --list 모드 ──
if [ "$LIST_ONLY" = true ]; then
    echo ""
    echo "=== 덤프 내용 목록 ==="
    dexec pg_restore --list < "$DUMP_FILE"
    exit 0
fi

# ── DB 준비 ──
if [ "$DATA_ONLY" = true ]; then
    echo ""
    echo "[2/5] DB 확인..."
    if db_exists; then
        echo "  $DB_NAME exists"
    else
        create_db
        echo "  $DB_NAME created"
    fi

    echo ""
    echo "[3/5] Django migration 적용..."
    drun_django python manage.py migrate --noinput

    echo ""
    echo "[4/5] 기존 앱 데이터 truncate..."
    dexec psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "
        DO \$\$
        DECLARE r RECORD;
        BEGIN
            FOR r IN
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename IN ($APP_DATA_TABLES_SQL)
            LOOP
                EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', r.tablename);
            END LOOP;
        END \$\$;
    "
    echo "  done"
else
    echo ""
    echo "[2/4] DB 초기화 (DROP -> CREATE)..."
    dexec psql -U "$DB_USER" -d postgres -c "
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();
    " > /dev/null 2>&1 || true

    dexec psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME;"
    create_db
    echo "  $DB_NAME created"

    echo ""
    echo "[3/4] Django migration 적용..."
    drun_django python manage.py migrate --noinput
fi

# ── 덤프 복원 ──
echo ""
if [ "$DATA_ONLY" = true ]; then
    echo "[5/5] 앱 데이터 복원 중..."
    dexec pg_restore -U "$DB_USER" -d "$DB_NAME" \
        --data-only --disable-triggers --no-owner --no-privileges \
        "${RESTORE_TABLE_ARGS[@]}" \
        < "$DUMP_FILE" 2>&1 | grep -v "already exists" || true
else
    echo "[4/4] 앱 데이터 복원 중..."
    dexec pg_restore -U "$DB_USER" -d "$DB_NAME" \
        --data-only --disable-triggers --no-owner --no-privileges \
        "${RESTORE_TABLE_ARGS[@]}" \
        < "$DUMP_FILE" 2>&1 | grep -v "already exists" || true
fi
echo "  done"

# ── 테이블 현황 ──
echo ""
echo "=== 테이블 현황 ==="
dexec psql -U "$DB_USER" -d "$DB_NAME" -c "
    SELECT t.tablename AS table_name,
           COALESCE(s.n_live_tup, 0) AS row_count
    FROM pg_tables t
    LEFT JOIN pg_stat_user_tables s ON s.relname = t.tablename
    WHERE t.schemaname = 'public'
      AND t.tablename IN ($APP_DATA_TABLES_SQL)
      AND COALESCE(s.n_live_tup, 0) > 0
    ORDER BY s.n_live_tup DESC;
"

echo ""
echo "=== 완료 ==="
echo "  스키마는 Django migrations 기준으로 동기화되었습니다."
echo ""
echo "complete!"
