import logging

from celery import shared_task

from bots.vault_writer import write_meeting_note

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    retry_backoff=True,
    max_retries=3,
    autoretry_for=(OSError,),  # retry only on transient IO (e.g. Drive mount hiccup)
)
def write_meeting_note_task(self, recording_id):
    """Best-effort: render + write the vault transcript note for a completed recording.

    Enqueued from RecordingManager.set_recording_transcription_complete. Failures are
    logged and never propagate to the transcription pipeline (that path fire-and-forgets
    this task), but OSErrors auto-retry with backoff in case the vault mount is briefly
    unavailable.
    """
    try:
        path = write_meeting_note(recording_id)
        if path:
            logger.info(f"write_meeting_note_task wrote {path} for recording {recording_id}")
    except OSError:
        raise  # let autoretry handle transient IO failures
    except Exception:
        logger.exception(f"write_meeting_note_task failed for recording {recording_id}")
