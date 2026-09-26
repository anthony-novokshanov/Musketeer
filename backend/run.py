# Equivalent to `uvicorn app.main:app --port 8000`, but on Windows forces a
# SelectorEventLoop: psycopg async can't use uvicorn's default ProactorEventLoop.
import asyncio
import selectors
import sys

import uvicorn

if __name__ == "__main__":
    server = uvicorn.Server(uvicorn.Config("app.main:app", port=8000))
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(server.serve())
