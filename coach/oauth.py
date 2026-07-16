"""Google OAuth web flow for multi-user authorization.

Each user authorizes their own Google Health account by:
1. Tapping "Login Google Health" in LINE → bot sends a login URL
2. User opens the URL in their browser → standard Google consent screen
3. After granting, Google redirects to /auth/google/callback
4. We exchange the code for tokens, store them in the users table

The `state` parameter carries a signed token containing the LINE userId so the
callback can associate the grant with the correct user.
"""

import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from coach import db
from coach.config import (
    GOOGLE_CLIENT_SECRET_FILE,
    GOOGLE_CLIENT_SECRET_WEB_FILE,
    GOOGLE_HEALTH_SCOPES,
    DATA_DIR,
)

log = logging.getLogger(__name__)

# Secret for signing the state token (prevents CSRF). Uses the channel secret
# or a dedicated ENCRYPTION_KEY if available.
def _get_state_secret() -> str:
    import os
    return os.environ.get("ENCRYPTION_KEY") or os.environ.get("LINE_CHANNEL_SECRET") or "dev-secret"


# Separator between the user_id and signature in the state token. Must be a
# URL-safe *unreserved* character (RFC 3986) so messaging clients like LINE keep
# the whole URL clickable — a '|' is NOT unreserved and gets cut off, breaking
# the link. Both the user_id (LINE 'U' + hex) and signature (hex) are
# alphanumeric, so '.' is a safe delimiter.
_STATE_SEP = "."


def _sign_state(user_id: str) -> str:
    """Create a signed state string: user_id.signature."""
    secret = _get_state_secret()
    sig = hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{user_id}{_STATE_SEP}{sig}"


def _verify_state(state: str) -> str | None:
    """Verify and extract user_id from a signed state. Returns user_id or None.

    Accepts both the current '.' separator and the legacy '|' separator so any
    login links generated before the fix still validate.
    """
    sep = _STATE_SEP if _STATE_SEP in state else ("|" if "|" in state else None)
    if sep is None:
        return None
    user_id, sig = state.rsplit(sep, 1)
    expected_sig = hmac.new(
        _get_state_secret().encode(), user_id.encode(), hashlib.sha256
    ).hexdigest()[:16]
    if hmac.compare_digest(sig, expected_sig):
        return user_id
    return None


def build_auth_url(user_id: str, redirect_uri: str) -> str:
    """Build the Google OAuth authorization URL for a user.

    redirect_uri: the full callback URL, e.g. https://coach.signagegold.co/auth/google/callback
    Uses the Web Application client (not Desktop) since this is a browser redirect flow.
    """
    # Prefer the web client; fall back to desktop client for backward compat
    client_file = GOOGLE_CLIENT_SECRET_WEB_FILE if GOOGLE_CLIENT_SECRET_WEB_FILE.exists() else GOOGLE_CLIENT_SECRET_FILE
    if not client_file.exists():
        raise RuntimeError(f"Missing OAuth client JSON ({client_file})")

    flow = Flow.from_client_secrets_file(
        str(client_file),
        scopes=GOOGLE_HEALTH_SCOPES,
        redirect_uri=redirect_uri,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=_sign_state(user_id),
    )
    return auth_url


def exchange_code(code: str, state: str, redirect_uri: str) -> tuple[str | None, str | None]:
    """Exchange the authorization code for tokens and store them.

    Returns (user_id, error_message). On success error_message is None.
    """
    # Verify state
    user_id = _verify_state(state)
    if not user_id:
        return None, "Invalid state parameter — authorization failed."

    # Exchange code for credentials
    try:
        client_file = GOOGLE_CLIENT_SECRET_WEB_FILE if GOOGLE_CLIENT_SECRET_WEB_FILE.exists() else GOOGLE_CLIENT_SECRET_FILE
        flow = Flow.from_client_secrets_file(
            str(client_file),
            scopes=GOOGLE_HEALTH_SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Exception as e:
        log.exception("token exchange failed")
        return user_id, f"Token exchange failed: {e}"

    # Store the token JSON in the users table
    token_json = creds.to_json()
    db.update_user(user_id, google_token_json=token_json)
    log.info("stored Google token for user %s", user_id)

    return user_id, None
