"""Google accounts for demo personas: Gmail (the demo inbox) and Calendar (free/busy + booking).

One OAuth client (GMAIL_CREDENTIALS); each persona signs in once with their own Google account:

    python -m app.google p_030        # Jordan's Google account (also the Gmail inbox)
    python -m app.google p_025        # Priya's Google account

Tokens are saved to GOOGLE_TOKEN_DIR/<person_id>.json (gitignored).
"""
import json
import os
import sys
from pathlib import Path

from app.config import settings

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events",
]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # Google may return scopes in another order


def _dir() -> Path:
    return Path(settings.GOOGLE_TOKEN_DIR)


def has_account(person_id: str) -> bool:
    return (_dir() / f"{person_id}.json").exists()


def account_email(person_id: str) -> str:
    return json.loads((_dir() / "accounts.json").read_text())[person_id]


def service(person_id: str, api: str, version: str):
    """Authorized Google API client acting as this person. Blocking; call via asyncio.to_thread."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = _dir() / f"{person_id}.json"
    if not path.exists():
        raise RuntimeError(f"no Google account for {person_id}; run: python -m app.google {person_id}")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return build(api, version, credentials=creds, cache_discovery=False)


def _sign_in(person_id: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = InstalledAppFlow.from_client_secrets_file(settings.GMAIL_CREDENTIALS, SCOPES).run_local_server(port=0)
    _dir().mkdir(parents=True, exist_ok=True)
    (_dir() / f"{person_id}.json").write_text(creds.to_json())
    email = service(person_id, "oauth2", "v2").userinfo().get().execute()["email"]
    accounts_path = _dir() / "accounts.json"
    accounts = json.loads(accounts_path.read_text()) if accounts_path.exists() else {}
    accounts[person_id] = email
    accounts_path.write_text(json.dumps(accounts, indent=1))
    print(f"Saved Google account {email} for {person_id}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m app.google <person_id>")
    _sign_in(sys.argv[1])
