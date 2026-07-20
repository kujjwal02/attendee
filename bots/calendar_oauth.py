"""In-app Google Calendar OAuth (self-hosted fork).

Server-side authorization-code flow for connecting a Google Calendar from the
dashboard (a "Connect Google Calendar" button), so the user never runs a script.
Reuses the existing django-allauth Google OAuth client (the same one used for
login) — its credentials live in a SocialApp row, not settings.

The dashboard views in projects_views.py drive this: build_authorize_url() ->
redirect the user to Google -> callback exchanges the code -> the refresh token +
client id/secret are exactly what bots.calendars_api_utils.create_calendar needs.
"""

import logging
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

# openid+email so we can name the connected account (used as the dedup key);
# calendar.readonly is what the sync task actually needs.
SCOPES = "openid email https://www.googleapis.com/auth/calendar.readonly"
AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_oauth_app():
    """The allauth Google SocialApp (client_id + secret), or None if not configured."""
    from allauth.socialaccount.models import SocialApp

    return SocialApp.objects.filter(provider="google").first()


def build_authorize_url(client_id, redirect_uri, state):
    """URL to send the user's browser to for Google consent.

    access_type=offline + prompt=consent are what make Google return a refresh_token
    (and re-issue one even if the user previously granted access).
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_tokens(code, redirect_uri, client_id, client_secret):
    """Exchange the authorization code for tokens. Returns the token JSON or raises."""
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def account_email(access_token):
    """Best-effort: the connected Google account's email (for the dedup key). None on failure."""
    try:
        resp = requests.get(USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("email")
    except Exception:
        logger.warning("Could not fetch Google account email for calendar connect", exc_info=True)
        return None
