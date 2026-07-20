"""Tests for the vault-writer note rendering (self-hosted fork)."""

from django.test import TestCase

from bots.vault_writer import _format_ts, _slugify, render_meeting_note, render_transcript_body


class VaultWriterRenderTest(TestCase):
    def test_slugify(self):
        self.assertEqual(_slugify("Weekly Sync: BYOB!"), "weekly-sync-byob")
        self.assertEqual(_slugify(""), "meeting")
        self.assertEqual(_slugify("   ---  "), "meeting")

    def test_format_ts(self):
        self.assertEqual(_format_ts(0), "[0:00]")
        self.assertEqual(_format_ts(65_000), "[1:05]")
        self.assertEqual(_format_ts(3_725_000), "[1:02:05]")
        self.assertEqual(_format_ts(None), "[?]")

    def test_transcript_merges_consecutive_speaker(self):
        utterances = [
            {"speaker": "Alice", "timestamp_ms": 0, "text": "Hello"},
            {"speaker": "Alice", "timestamp_ms": 2000, "text": "how are you"},
            {"speaker": "Bob", "timestamp_ms": 5000, "text": "Good thanks"},
            {"speaker": "Alice", "timestamp_ms": 8000, "text": ""},  # empty skipped
        ]
        body = render_transcript_body(utterances)
        self.assertEqual(
            body,
            "**[0:00] Alice:** Hello how are you\n\n**[0:05] Bob:** Good thanks",
        )

    def test_transcript_empty(self):
        self.assertEqual(render_transcript_body([]), "_(no transcript captured)_")

    def test_render_note_quotes_title_and_includes_sections(self):
        note = render_meeting_note(
            title="Sync: Q3 Plan",
            created_iso="2026-07-20 14:30",
            updated_iso="2026-07-20",
            meeting_url="https://meet.google.com/abc-defg-hij",
            attendees=["Alice", "Bob (bob@example.com)"],
            dashboard_url="https://attendee.ujjwalk.dev/projects/p/bots/b",
            drive_url="https://drive.google.com/drive/search?q=bot_xyz-rec_1.mp4",
            bot_object_id="bot_xyz",
            utterances=[{"speaker": "Alice", "timestamp_ms": 0, "text": "Hi"}],
        )
        # colon-containing title must be double-quoted for Obsidian YAML
        self.assertIn('title: "Meeting — Sync: Q3 Plan"', note)
        self.assertIn("tags: [area/meetings, topic/transcript, status/active]", note)
        self.assertIn("bot: bot_xyz", note)
        self.assertIn("## Attendees", note)
        self.assertIn("- Alice", note)
        self.assertIn("- Bob (bob@example.com)", note)
        self.assertIn("[open in Google Drive](https://drive.google.com/drive/search?q=bot_xyz-rec_1.mp4)", note)
        self.assertIn("[open in Attendee](https://attendee.ujjwalk.dev/projects/p/bots/b)", note)
        self.assertIn("## Transcript", note)
        self.assertIn("**[0:00] Alice:** Hi", note)

    def test_render_note_omits_optional_fields(self):
        note = render_meeting_note(
            title="2026-07-20 14:30",
            created_iso="2026-07-20 14:30",
            updated_iso="2026-07-20",
            meeting_url="",
            attendees=[],
            dashboard_url="",
            drive_url="",
            bot_object_id="bot_abc",
            utterances=[],
        )
        self.assertNotIn("meeting_url:", note)
        self.assertNotIn("Recording:", note)
        self.assertNotIn("Video:", note)
        self.assertIn("## Attendees\n\n_(none captured)_", note)
        self.assertIn("_(no transcript captured)_", note)
