"""Environment and storage policy enforcement for backend startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


VALID_ENVIRONMENTS = {"test", "development", "production"}
VALID_BACKENDS = {"sqlite", "postgresql"}


@dataclass(frozen=True)
class StoragePolicy:
    environment: str
    backend: str
    production_ready: bool
    database_url_configured: bool


def enforce_storage_policy(environment: str | None = None,
                           backend: str | None = None,
                           database_url: str | None = None) -> StoragePolicy:
    resolved_environment = (environment or os.getenv("LCU_ENV", "development")).strip().lower()
    resolved_backend = (backend or os.getenv("LCU_STORAGE_BACKEND", "sqlite")).strip().lower()
    resolved_url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")

    if resolved_environment not in VALID_ENVIRONMENTS:
        raise RuntimeError("LCU_ENV must be test, development, or production")
    if resolved_backend not in VALID_BACKENDS:
        raise RuntimeError("LCU_STORAGE_BACKEND must be sqlite or postgresql")
    if resolved_environment == "production":
        if resolved_backend != "postgresql":
            raise RuntimeError("Production requires LCU_STORAGE_BACKEND=postgresql; SQLite is not permitted")
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
            raise RuntimeError("Production requires a PostgreSQL DATABASE_URL")
        raise RuntimeError("PostgreSQL production storage is not implemented; production startup is blocked")
    if resolved_backend == "postgresql":
        raise RuntimeError("PostgreSQL storage is not implemented yet")
    return StoragePolicy(
        environment=resolved_environment,
        backend=resolved_backend,
        production_ready=False,
        database_url_configured=bool(resolved_url),
    )
