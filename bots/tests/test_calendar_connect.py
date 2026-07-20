"""Integration tests for the in-app Google Calendar OAuth connect flow (self-hosted fork)."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from allauth.socialaccount.models import SocialApp
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Calendar, CalendarPlatform, Project


class CalendarConnectGoogleTest(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org", centicredits=10000)
        self.user = User.objects.create_user(username="u", email="u@example.com", password="pw123456789", role=UserRole.ADMIN, organization=self.org)
        self.project = Project.objects.create(name="P", organization=self.org)
        self.app = SocialApp.objects.create(provider="google", name="google", client_id="cid.apps.googleusercontent.com", secret="sekret")
        self.client = Client()
        self.client.force_login(self.user)

    def test_connect_redirects_to_google_and_stores_state(self):
        resp = self.client.get(reverse("bots:calendar-connect-google", args=[self.project.object_id]) + "?policy=organizer")
        self.assertEqual(resp.status_code, 302)
        parsed = urlparse(resp["Location"])
        self.assertEqual(parsed.netloc, "accounts.google.com")
        q = parse_qs(parsed.query)
        self.assertEqual(q["client_id"][0], "cid.apps.googleusercontent.com")
        self.assertEqual(q["access_type"][0], "offline")
        self.assertEqual(q["prompt"][0], "consent")
        self.assertIn("calendar.readonly", q["scope"][0])
        self.assertTrue(q["redirect_uri"][0].endswith("/projects/calendars/oauth/google/callback"))
        # state stored in session, policy captured
        sess = self.client.session["gcal_oauth"]
        self.assertEqual(sess["state"], q["state"][0])
        self.assertEqual(sess["policy"], "organizer")

    def test_connect_without_socialapp_shows_message(self):
        SocialApp.objects.all().delete()
        resp = self.client.get(reverse("bots:calendar-connect-google", args=[self.project.object_id]))
        self.assertRedirects(resp, reverse("bots:project-calendars", args=[self.project.object_id]), fetch_redirect_response=False)
        self.assertNotIn("gcal_oauth", self.client.session)

    def test_callback_bad_state_errors(self):
        # No session set up -> invalid
        resp = self.client.get(reverse("bots:calendar-connect-google-callback") + "?state=nope&code=abc")
        self.assertEqual(resp.status_code, 302)

    def test_callback_creates_calendar_with_policy_metadata(self):
        # Prime the session as the connect view would
        start = self.client.get(reverse("bots:calendar-connect-google", args=[self.project.object_id]) + "?policy=participant")
        state = self.client.session["gcal_oauth"]["state"]

        with patch("bots.calendar_oauth.exchange_code_for_tokens", return_value={"refresh_token": "rt-123", "access_token": "at-123"}), \
             patch("bots.calendar_oauth.account_email", return_value="me@work.com"), \
             patch("bots.tasks.sync_calendar_task.enqueue_sync_calendar_task") as enq:
            resp = self.client.get(reverse("bots:calendar-connect-google-callback") + f"?state={state}&code=authcode")

        self.assertRedirects(resp, reverse("bots:project-calendars", args=[self.project.object_id]), fetch_redirect_response=False)
        cal = Calendar.objects.get(project=self.project)
        self.assertEqual(cal.platform, CalendarPlatform.GOOGLE)
        self.assertEqual(cal.client_id, "cid.apps.googleusercontent.com")
        self.assertEqual(cal.deduplication_key, "google:me@work.com")
        self.assertEqual(cal.metadata["auto_dispatch"], {"enabled": True, "policy": "participant"})
        creds = cal.get_credentials()
        self.assertEqual(creds["refresh_token"], "rt-123")
        self.assertEqual(creds["client_secret"], "sekret")
        enq.assert_called_once()

    def test_callback_no_refresh_token_errors(self):
        self.client.get(reverse("bots:calendar-connect-google", args=[self.project.object_id]))
        state = self.client.session["gcal_oauth"]["state"]
        with patch("bots.calendar_oauth.exchange_code_for_tokens", return_value={"access_token": "at-123"}):
            resp = self.client.get(reverse("bots:calendar-connect-google-callback") + f"?state={state}&code=authcode")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Calendar.objects.filter(project=self.project).exists())
