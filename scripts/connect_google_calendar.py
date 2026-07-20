#!/usr/bin/env python3
"""Connect a Google Calendar to self-hosted Attendee for auto-dispatch.

Attendee has no "Connect Calendar" button — it expects a Google OAuth *refresh
token*. This one-time helper runs the Google consent flow (read-only calendar
scope) in your browser, obtains a refresh token, and registers the calendar via
Attendee's REST API (embedding the auto-dispatch policy in the calendar metadata).

Prereqs (one-time, in Google Cloud console, project e.g. `ujjwalk-homelab`):
  1. Enable the "Google Calendar API".
  2. Have an OAuth 2.0 Client of type "Desktop app" (you can reuse the rclone one).
     Note its client_id + client_secret.
  3. On the OAuth consent screen, add scope
     `https://www.googleapis.com/auth/calendar.readonly` (or add yourself as a test
     user). An unverified app just shows an "unsafe" warning you can proceed past.

Run:
  pip install google-auth-oauthlib requests
  python scripts/connect_google_calendar.py \
      --client-id  YOUR_CLIENT_ID.apps.googleusercontent.com \
      --client-secret GOCSPX-... \
      --attendee-base https://attendee.ujjwalk.dev \
      --api-key  YOUR_ATTENDEE_API_KEY \
      --policy participant \
      --dedup   work

  # or read client id/secret from a downloaded OAuth client JSON:
  python scripts/connect_google_calendar.py --client-secrets client_secret.json ...

`--policy` is the default policy stored on this calendar (participant | organizer |
accepted | keyword | all). Add `--keyword '[rec]'` for the keyword policy. Re-run with
a different `--dedup` (e.g. `personal`) to connect another calendar.
"""

import argparse
import json
import sys

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def parse_args():
    p = argparse.ArgumentParser(description="Connect a Google Calendar to Attendee for auto-dispatch.")
    p.add_argument("--client-secrets", help="Path to an OAuth client JSON (installed/desktop app).")
    p.add_argument("--client-id", help="OAuth client ID (if not using --client-secrets).")
    p.add_argument("--client-secret", help="OAuth client secret (if not using --client-secrets).")
    p.add_argument("--attendee-base", required=True, help="Attendee base URL, e.g. https://attendee.ujjwalk.dev")
    p.add_argument("--api-key", required=True, help="Attendee API key (Authorization: Token ...).")
    p.add_argument("--policy", default="participant", choices=["participant", "organizer", "accepted", "keyword", "all"])
    p.add_argument("--keyword", default="", help="Title substring for --policy keyword.")
    p.add_argument("--dedup", default="work", help="Deduplication key for this calendar (e.g. work / personal).")
    p.add_argument("--no-enable", action="store_true", help="Register the calendar but leave auto_dispatch.enabled=false.")
    return p.parse_args()


def get_client_config(args):
    if args.client_secrets:
        with open(args.client_secrets) as fh:
            data = json.load(fh)
        # normalize to the {"installed": {...}} shape InstalledAppFlow expects
        if "installed" in data or "web" in data:
            return data
        raise SystemExit("client-secrets JSON must contain an 'installed' or 'web' key.")
    if not (args.client_id and args.client_secret):
        raise SystemExit("Provide either --client-secrets or both --client-id and --client-secret.")
    return {
        "installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def main():
    args = parse_args()
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("Missing deps. Run: pip install google-auth-oauthlib requests")
    import requests

    client_config = get_client_config(args)
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # access_type=offline + prompt=consent are what force Google to return a refresh_token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        raise SystemExit("No refresh_token returned. Revoke prior access at myaccount.google.com/permissions and retry (prompt=consent forces a fresh grant).")

    client_id = client_config["installed"]["client_id"]
    client_secret = client_config["installed"]["client_secret"]

    metadata = {"auto_dispatch": {"enabled": not args.no_enable, "policy": args.policy}}
    if args.policy == "keyword":
        metadata["auto_dispatch"]["keyword"] = args.keyword

    payload = {
        "platform": "google",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "deduplication_key": args.dedup,
        "metadata": metadata,
    }

    url = args.attendee_base.rstrip("/") + "/api/v1/calendars"
    resp = requests.post(url, json=payload, headers={"Authorization": f"Token {args.api_key}", "Content-Type": "application/json"}, timeout=30)
    print(f"POST {url} -> {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)
    if resp.status_code not in (200, 201):
        sys.exit(1)
    print("\nCalendar connected. It will sync within ~a minute; auto-dispatch books bots for matching upcoming events.")


if __name__ == "__main__":
    main()
