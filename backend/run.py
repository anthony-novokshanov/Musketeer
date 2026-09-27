# Equivalent to `uvicorn app.main:app --port 8000`, but on Windows forces a
# SelectorEventLoop: psycopg async can't use uvicorn's default ProactorEventLoop.
import os
import socket
import sys

import uvicorn

from app.db import run_async

PORT = 8000

if __name__ == "__main__":
    # Claim the port before the app starts: a second copy would otherwise connect to Slack first,
    # and Slack splits clicks and messages between every connected copy (some would hit stale code).
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
    try:
        sock.bind(("127.0.0.1", PORT))
    except OSError:
        sys.exit(f"Port {PORT} is taken: Musketeer is already running. Stop it first.")
    run_async(uvicorn.Server(uvicorn.Config("app.main:app")).serve(sockets=[sock]))
    os._exit(0)  # don't let leftover threads (Gmail, Slack socket) keep a dead server alive
