from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from app.config import settings


trust_certificate = (
    "yes" if settings.db_trust_server_certificate else "no"
)

connection_string = (
    f"DRIVER={{{settings.db_driver}}};"
    f"SERVER={settings.db_server};"
    f"DATABASE={settings.db_name};"
    "Trusted_Connection=yes;"
    "Encrypt=yes;"
    f"TrustServerCertificate={trust_certificate};"
)

connection_url = URL.create(
    "mssql+pyodbc",
    query={"odbc_connect": connection_string},
)

engine = create_engine(
    connection_url,
    pool_pre_ping=True,
    connect_args={"timeout": 10},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db():
    with SessionLocal() as session:
        yield session