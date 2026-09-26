# Equivalent to `uvicorn app.main:app --port 8000`, but on Windows forces a
# SelectorEventLoop: psycopg async can't use uvicorn's default ProactorEventLoop.
import uvicorn

from app.db import run_async

if __name__ == "__main__":
    run_async(uvicorn.Server(uvicorn.Config("app.main:app", port=8000)).serve())
