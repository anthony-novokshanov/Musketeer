"""Add the temporal expertise graph tables/columns to an existing database. Safe to rerun; keeps all data.

    python -m scripts.migrate
"""
from app.db import migrate, run_async

if __name__ == "__main__":
    run_async(migrate())
    print("expertise schema applied")
