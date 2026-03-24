import os

from sqlalchemy import create_engine


def build_database_url() -> str:
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured

    user = os.getenv("POSTGRES_USER", "mungnyang")
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "tailtalk_db")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


DATABASE_URL = build_database_url()

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
)
