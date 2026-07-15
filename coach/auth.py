"""One-time Google OAuth flow + credential loading.

Run once:  python -m coach.auth
  (in Docker: docker compose run --rm -p 8765:8765 coach python -m coach.auth)

Prereq: download the OAuth client JSON (Desktop app type) from the Google
Cloud console and save it as data/google_client_secret.json.
The resulting token (with refresh token) is stored in data/google_token.json
and auto-refreshes on every later run.
"""

import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from coach.config import GOOGLE_CLIENT_SECRET_FILE, GOOGLE_HEALTH_SCOPES, GOOGLE_TOKEN_FILE


def run_auth_flow() -> None:
    if not GOOGLE_CLIENT_SECRET_FILE.exists():
        sys.exit(
            f"Missing {GOOGLE_CLIENT_SECRET_FILE}.\n"
            "Create an OAuth client (type: Desktop app) in the Google Cloud console "
            "and save the downloaded JSON to that path."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(GOOGLE_CLIENT_SECRET_FILE), scopes=GOOGLE_HEALTH_SCOPES
    )
    # When running in Docker, bind to 0.0.0.0 so the port-mapped redirect
    # can reach the server. Detect via COACH_DATA_DIR=/app/data (set in Dockerfile).
    import os
    host = "0.0.0.0" if os.environ.get("COACH_DATA_DIR") == "/app/data" else "localhost"
    creds = flow.run_local_server(
        host=host, port=8765, open_browser=False,
        authorization_prompt_message="\nOpen this URL in your browser:\n{url}\n",
    )
    GOOGLE_TOKEN_FILE.write_text(creds.to_json())
    print(f"Saved credentials to {GOOGLE_TOKEN_FILE}")


def get_credentials() -> Credentials:
    """Load stored credentials, refreshing the access token if expired."""
    if not GOOGLE_TOKEN_FILE.exists():
        raise RuntimeError("No Google token found - run `python -m coach.auth` first.")
    creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_FILE), GOOGLE_HEALTH_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        GOOGLE_TOKEN_FILE.write_text(creds.to_json())
    return creds


if __name__ == "__main__":
    run_auth_flow()
