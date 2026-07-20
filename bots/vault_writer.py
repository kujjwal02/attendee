"""Vault-writer (self-hosted fork).

When a recording's transcription completes, render a Markdown transcript note and
write it into the ~/knowledge vault (bind-mounted at settings.VAULT_NOTE_DIR).
gitwatch + Syncthing on the hub then propagate the note to every device.

The rendering functions are pure (easy to unit-test); write_meeting_note() does the
DB reads + file IO and is meant to be called from a best-effort Celery task, so any
failure here must never break the transcription pipeline.
"""

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)


def _slugify(text: str, max_len: int = 60) -> str:
    """Filesystem/URL-safe slug: lowercase, non-alphanumerics collapsed to '-'."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].strip("-") or "meeting"


def _format_ts(ms) -> str:
    """Milliseconds (relative to recording start) -> [H:MM:SS] or [M:SS]."""
    if ms is None:
        return "[?]"
    total_seconds = int(ms) // 1000
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"[{hours}:{minutes:02d}:{seconds:02d}]"
    return f"[{minutes}:{seconds:02d}]"


def _yaml_quote(value: str) -> str:
    """Double-quote a YAML scalar (vault rule: quote anything with ': ', '#', etc.)."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_transcript_body(utterances) -> str:
    """Render the speaker-labelled, timestamped transcript.

    `utterances` is an iterable of dicts: {speaker, timestamp_ms, text}, already
    ordered by timestamp_ms. Consecutive lines from the same speaker are merged into
    one block, prefixed with the block's first timestamp.
    """
    lines = []
    current_speaker = None
    current_ts = None
    current_texts = []

    def flush():
        if current_texts:
            lines.append(f"**{_format_ts(current_ts)} {current_speaker}:** " + " ".join(current_texts))

    for u in utterances:
        text = (u.get("text") or "").strip()
        if not text:
            continue
        speaker = u.get("speaker") or "Unknown"
        if speaker != current_speaker:
            flush()
            current_speaker = speaker
            current_ts = u.get("timestamp_ms")
            current_texts = [text]
        else:
            current_texts.append(text)
    flush()

    return "\n\n".join(lines) if lines else "_(no transcript captured)_"


def render_meeting_note(*, title, created_iso, updated_iso, meeting_url, participants, dashboard_url, bot_object_id, utterances) -> str:
    """Return the full Markdown note (frontmatter + header block + transcript)."""
    fm_title = _yaml_quote(f"Meeting — {title}")
    front = [
        "---",
        f"title: {fm_title}",
        "tags: [area/meetings, topic/transcript, status/active]",
        f"created: {created_iso}",
        f"updated: {updated_iso}",
        "source: attendee",
        f"bot: {bot_object_id}",
    ]
    if meeting_url:
        front.append(f"meeting_url: {_yaml_quote(meeting_url)}")
    front.append("---")

    people = ", ".join(participants) if participants else "—"
    header = [
        f"# Meeting — {title}",
        "",
        f"- **When:** {created_iso}",
        f"- **Participants:** {people}",
    ]
    if meeting_url:
        header.append(f"- **Meeting URL:** {meeting_url}")
    if dashboard_url:
        header.append(f"- **Recording:** [open in Attendee]({dashboard_url})")

    body = render_transcript_body(utterances)

    return "\n".join(front) + "\n\n" + "\n".join(header) + "\n\n## Transcript\n\n" + body + "\n"


def write_meeting_note(recording_id: int) -> str | None:
    """Load the recording's data, render a note, and write it into the vault.

    Returns the written file path, or None if disabled/skipped. Raises on IO errors
    so the caller (Celery task) can log/retry; never called on the transcription path
    directly.
    """
    import os

    from bots.models import Participant, Recording, Utterance

    if not settings.VAULT_NOTE_ENABLED:
        return None

    recording = Recording.objects.select_related("bot", "bot__project").get(id=recording_id)
    bot = recording.bot
    project = bot.project

    utterance_qs = Utterance.objects.filter(recording=recording, async_transcription__isnull=True).exclude(transcription__isnull=True).select_related("participant").order_by("timestamp_ms")

    # Normalize timestamps to be relative to recording start. Closed-caption utterances
    # store an absolute epoch-ms timestamp; per-participant-audio ones are already relative
    # to the start. Epoch values (>= ~1e12 ms) get the recording start subtracted; small
    # values are left as-is. Clamp to 0 so a caption that started just before the recording
    # buffer doesn't render negative.
    EPOCH_MS_THRESHOLD = 1_000_000_000_000
    start_ms = recording.first_buffer_timestamp_ms or 0

    def _relative_ms(ts):
        if ts is None:
            return None
        if ts >= EPOCH_MS_THRESHOLD:
            return max(0, ts - start_ms)
        return ts

    utterances = [
        {
            "speaker": u.participant.full_name or u.participant.uuid,
            "timestamp_ms": _relative_ms(u.timestamp_ms),
            "text": (u.transcription or {}).get("transcript", ""),
        }
        for u in utterance_qs
    ]

    participants = list(
        Participant.objects.filter(bot=bot, is_the_bot=False).exclude(full_name__isnull=True).exclude(full_name="").order_by("created_at").values_list("full_name", flat=True).distinct()
    )

    # Meeting title: prefer the bot's name if it's meaningful, else the date.
    created_dt = bot.created_at
    created_iso = created_dt.strftime("%Y-%m-%d %H:%M")
    bot_name = (bot.name or "").strip()
    generic = {"", "attendee", "bot", "meeting bot"}
    title = bot_name if bot_name.lower() not in generic else created_dt.strftime("%Y-%m-%d %H:%M")

    dashboard_url = ""
    if settings.VAULT_NOTE_BASE_URL:
        dashboard_url = f"{settings.VAULT_NOTE_BASE_URL}/projects/{project.object_id}/bots/{bot.object_id}"

    content = render_meeting_note(
        title=title,
        created_iso=created_iso,
        updated_iso=created_dt.strftime("%Y-%m-%d"),
        meeting_url=bot.meeting_url,
        participants=participants,
        dashboard_url=dashboard_url,
        bot_object_id=bot.object_id,
        utterances=utterances,
    )

    filename = f"{created_dt.strftime('%Y-%m-%d-%H%M')}-{_slugify(title)}-{bot.object_id}.md"
    os.makedirs(settings.VAULT_NOTE_DIR, exist_ok=True)
    path = os.path.join(settings.VAULT_NOTE_DIR, filename)
    # Atomic-ish write: temp file + rename so gitwatch never sees a half-written note.
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp_path, path)

    logger.info(f"Vault note written for recording {recording.object_id}: {path} ({len(utterances)} utterances)")
    return path
