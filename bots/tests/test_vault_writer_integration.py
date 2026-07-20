"""DB-backed tests for write_meeting_note (self-hosted fork).

Covers the calendar-event title -> filename slug, the merged attendees list, and the
Google Drive video link, using real Bot/Recording/CalendarEvent/Participant rows.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization
from bots.models import (
    Bot,
    Calendar,
    CalendarEvent,
    CalendarPlatform,
    CalendarStates,
    Participant,
    Project,
    Recording,
    RecordingStates,
    RecordingTranscriptionStates,
)
from bots.vault_writer import write_meeting_note


@override_settings(VAULT_NOTE_ENABLED=True, VAULT_NOTE_BASE_URL="https://attendee.ujjwalk.dev", VAULT_NOTE_DRIVE_SEARCH_URL="https://drive.google.com/drive/search?q=")
class WriteMeetingNoteTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org", centicredits=10000)
        self.project = Project.objects.create(name="P", organization=self.org)
        self.calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, state=CalendarStates.CONNECTED, client_id="cid")
        now = timezone.now()
        self.event = CalendarEvent.objects.create(
            calendar=self.calendar,
            platform_uuid="evt-1",
            meeting_url="https://meet.google.com/abc-defg-hij",
            start_time=now,
            end_time=now + timedelta(hours=1),
            name="Weekly Sync: BYOB",
            attendees=[{"email": "alice@x.com", "name": "Alice Smith"}, {"email": "carol@x.com", "name": "Carol"}],
            raw={},
        )
        self.bot = Bot.objects.create(project=self.project, meeting_url="https://meet.google.com/abc-defg-hij", name="Ujjwal's Notetaker", calendar_event=self.event)
        self.recording = Recording.objects.create(bot=self.bot, recording_type=1, transcription_type=1, state=RecordingStates.COMPLETE, transcription_state=RecordingTranscriptionStates.COMPLETE)
        self.recording.file.name = f"{self.bot.object_id}-{self.recording.object_id}.mp4"
        self.recording.save()
        # Alice actually showed up (observed by the bot); Carol only invited.
        Participant.objects.create(bot=self.bot, uuid="p1", full_name="Alice Smith", is_the_bot=False)
        Participant.objects.create(bot=self.bot, uuid="p2", full_name="Dave Jones", is_the_bot=False)

    def _write(self, tmp_path):
        with override_settings(VAULT_NOTE_DIR=str(tmp_path)):
            return write_meeting_note(self.recording.id)

    def test_filename_uses_calendar_title_slug(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = self._write(d)
            self.assertIsNotNone(path)
            fname = path.rsplit("/", 1)[-1]
            # <date>-<time>-<slug>-<bot-id>.md ; slug comes from the calendar event title
            self.assertIn("-weekly-sync-byob-", fname)
            self.assertTrue(fname.endswith(f"{self.bot.object_id}.md"))

    def test_note_content_has_title_attendees_and_drive_link(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = self._write(d)
            with open(path, encoding="utf-8") as fh:
                note = fh.read()
        self.assertIn('title: "Meeting — Weekly Sync: BYOB"', note)
        self.assertIn("# Meeting — Weekly Sync: BYOB", note)
        # observed participants first, then invitees not already seen
        self.assertIn("- Alice Smith", note)  # observed (deduped against invitee)
        self.assertIn("- Dave Jones", note)  # observed only
        self.assertIn("- Carol (carol@x.com)", note)  # invited only
        # Alice must not be duplicated as an invitee line
        self.assertNotIn("Alice Smith (alice@x.com)", note)
        # Drive video link points at the recording filename
        self.assertIn(f"https://drive.google.com/drive/search?q={self.bot.object_id}-{self.recording.object_id}.mp4", note)
