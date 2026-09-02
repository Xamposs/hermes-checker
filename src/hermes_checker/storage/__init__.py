"""Storage package — SQLite-backed persistence with simple schema migrations.

We intentionally keep this self-contained: no ORM, no migration framework.
``Database`` opens a SQLite file (default ``~/.hermes-checker/hermes-checker.db``),
applies pending schema migrations, and exposes the small set of typed writer
methods the collector / dashboard need.
"""
from .database import Database, DatabasePaths
from .schema import SCHEMA_VERSION, apply_migrations

__all__ = ["Database", "DatabasePaths", "SCHEMA_VERSION", "apply_migrations"]