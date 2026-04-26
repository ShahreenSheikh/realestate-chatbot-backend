"""
Handles Google credentials for both local and Railway deployment.
- Local: reads from service_account.json file
- Railway: reads from GOOGLE_CREDENTIALS_JSON environment variable
"""
import os
import json
import tempfile
from google.oauth2.service_account import Credentials


def get_credentials(scopes: list) -> Credentials:
    """
    Returns Google credentials regardless of environment.
    Works both locally (file) and on Railway (env variable).
    """
    # ── Option 1: Environment variable (Railway / production) ─────────────────
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        try:
            creds_dict = json.loads(creds_json)
            return Credentials.from_service_account_info(creds_dict, scopes=scopes)
        except Exception as e:
            print(f"[Credentials] Failed to load from env var: {e}")

    # ── Option 2: File (local development) ────────────────────────────────────
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    if os.path.exists(sa_file):
        return Credentials.from_service_account_file(sa_file, scopes=scopes)

    raise Exception(
        "No Google credentials found. "
        "Set GOOGLE_CREDENTIALS_JSON env var or place service_account.json in the backend folder."
    )
