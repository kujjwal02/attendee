"""Auto-dispatch (self-hosted fork).

Attendee natively syncs calendars, schedules bots (join_at + BotStates.SCHEDULED),
and auto-joins them via run_scheduler — but it deliberately leaves "which calendar
event deserves a bot" to the API consumer. This module is that decision layer: it
scans synced CalendarEvents and books a bot for the ones matching a configurable
policy. Reschedules/cancellations are then handled natively by
sync_bots_for_calendar_event (the bot is linked to the event).

Called each cycle from run_scheduler. Policy is resolved per-calendar (so a work and
a personal calendar can differ) via Calendar.metadata["auto_dispatch"], falling back
to the global AUTO_DISPATCH_* settings.

Policies:
  - "participant" (default): book unless *I* declined the event — i.e. any meeting I'm
    part of (organizer or a non-declined attendee, or my own solo event with a link).
  - "organizer": only events I organize.
  - "accepted": only events I've explicitly accepted (or organize).
  - "keyword": only events whose title contains AUTO_DISPATCH_KEYWORD.
  - "all": every event that has a meeting URL.
Every policy also requires a meeting URL and a non-deleted, upcoming event.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

VALID_POLICIES = {"participant", "organizer", "accepted", "keyword", "all"}


# --- per-platform "what did I do with this event" helpers -------------------
# Google stores the connected user as the attendee/organizer with "self": true.
# Microsoft Graph (/me/calendarView) exposes the user's own answer at top-level
# "responseStatus" and a boolean "isOrganizer".


def _google_self_attendee(raw):
    for a in raw.get("attendees", []) or []:
        if a.get("self"):
            return a
    return None


def _is_organizer(raw, platform):
    if platform == "microsoft":
        return raw.get("isOrganizer") is True
    if raw.get("organizer", {}).get("self") is True:
        return True
    # Google without an explicit organizer.self but where my attendee entry is the organizer
    self_att = _google_self_attendee(raw)
    return bool(self_att and self_att.get("organizer"))


def _my_response(raw, platform):
    """Return the connected user's response: accepted/declined/tentative/needsAction/None."""
    if platform == "microsoft":
        resp = (raw.get("responseStatus") or {}).get("response")
        # Graph values: none, organizer, tentativelyAccepted, accepted, declined, notResponded
        return {
            "organizer": "accepted",
            "accepted": "accepted",
            "tentativelyAccepted": "tentative",
            "declined": "declined",
            "notResponded": "needsAction",
            "none": None,
        }.get(resp, None)
    self_att = _google_self_attendee(raw)
    if self_att:
        return self_att.get("responseStatus")  # accepted/declined/tentative/needsAction
    return None


def _is_declined(raw, platform):
    return _my_response(raw, platform) == "declined"


def _is_accepted(raw, platform):
    return _is_organizer(raw, platform) or _my_response(raw, platform) == "accepted"


def event_matches_policy(event, policy, keyword) -> bool:
    """Pure predicate: does this CalendarEvent qualify for a bot under `policy`?"""
    if event.is_deleted or not event.meeting_url:
        return False

    raw = event.raw or {}
    platform = event.calendar.platform

    if policy == "all":
        return True
    if policy == "keyword":
        return bool(keyword) and keyword.lower() in (event.name or "").lower()
    if policy == "organizer":
        return _is_organizer(raw, platform)
    if policy == "accepted":
        return _is_accepted(raw, platform)
    # default: "participant" — anything I haven't declined
    return not _is_declined(raw, platform)


def resolve_policy(calendar):
    """Per-calendar config (Calendar.metadata['auto_dispatch']) over global defaults.

    Returns (enabled, policy, keyword).
    """
    cfg = {}
    if isinstance(calendar.metadata, dict):
        cfg = calendar.metadata.get("auto_dispatch") or {}

    enabled = cfg.get("enabled", settings.AUTO_DISPATCH_ENABLED)
    policy = cfg.get("policy") or settings.AUTO_DISPATCH_POLICY
    if policy not in VALID_POLICIES:
        logger.warning(f"Calendar {calendar.object_id}: invalid auto_dispatch policy {policy!r}, defaulting to 'participant'")
        policy = "participant"
    keyword = cfg.get("keyword", settings.AUTO_DISPATCH_KEYWORD)
    return bool(enabled), policy, keyword


def book_bots_for_calendar(calendar, now=None) -> int:
    """Book bots for this calendar's upcoming, policy-matching events. Returns count booked."""
    from bots.bots_api_utils import BotCreationSource, create_bot
    from bots.models import Bot, CalendarEvent

    enabled, policy, keyword = resolve_policy(calendar)
    if not enabled:
        return 0

    now = now or timezone.now()
    horizon = now + timedelta(hours=settings.AUTO_DISPATCH_HORIZON_HOURS)
    # A small negative floor so an event that just started (or the scheduler was briefly
    # down) still books; the scheduler's own ±5min window decides launch.
    lower = now - timedelta(minutes=5)

    events = CalendarEvent.objects.filter(
        calendar=calendar,
        is_deleted=False,
        meeting_url__isnull=False,
        start_time__gte=lower,
        start_time__lte=horizon,
    ).exclude(meeting_url="")

    booked = 0
    for event in events:
        # Skip if a bot already exists for this event (native sync then owns reschedule/cancel).
        if Bot.objects.filter(calendar_event=event).exists():
            continue
        if not event_matches_policy(event, policy, keyword):
            continue

        data = {
            "calendar_event_id": event.object_id,
            "bot_name": settings.AUTO_DISPATCH_BOT_NAME,
            "deduplication_key": f"cal-{event.object_id}",
        }
        bot, error = create_bot(data, BotCreationSource.SCHEDULER, calendar.project)
        if error:
            logger.warning(f"Auto-dispatch: failed to book bot for event {event.object_id} ({event.name!r}): {error}")
            continue
        booked += 1
        logger.info(f"Auto-dispatch: booked bot {bot.object_id} for event {event.object_id} ({event.name!r}) at {event.start_time.isoformat()} [policy={policy}]")

    return booked


def run_auto_dispatch(now=None) -> int:
    """Book bots across all connected calendars. Returns total booked. Best-effort per calendar."""
    from bots.models import Calendar, CalendarStates

    total = 0
    for calendar in Calendar.objects.filter(state=CalendarStates.CONNECTED):
        try:
            total += book_bots_for_calendar(calendar, now=now)
        except Exception:
            logger.exception(f"Auto-dispatch failed for calendar {calendar.object_id}")
    return total
