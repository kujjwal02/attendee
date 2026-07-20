"""Tests for cancelling a scheduled bot (self-hosted fork)."""

from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Organization, User, UserRole
from bots.models import Bot, BotEventManager, BotEventTypes, BotStates, Project


class CancelBotTest(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org", centicredits=10000)
        self.user = User.objects.create_user(username="u", email="u@example.com", password="pw123456789", role=UserRole.ADMIN, organization=self.org)
        self.project = Project.objects.create(name="P", organization=self.org)
        self.client = Client()
        self.client.force_login(self.user)

    def _bot(self, state=BotStates.SCHEDULED):
        return Bot.objects.create(project=self.project, meeting_url="https://meet.google.com/abc-defg-hij", name="Notetaker", state=state)

    def test_cancel_scheduled_bot_moves_to_cancelled(self):
        bot = self._bot(BotStates.SCHEDULED)
        resp = self.client.post(reverse("bots:cancel-bot", args=[self.project.object_id, bot.object_id]))
        self.assertEqual(resp.status_code, 302)
        bot.refresh_from_db()
        self.assertEqual(bot.state, BotStates.CANCELLED)

    def test_cancelled_bot_is_terminal(self):
        # A cancelled bot must count as post-meeting (excluded from concurrency, never relaunched).
        self.assertIn(BotStates.CANCELLED, BotStates.post_meeting_states())

    def test_cannot_cancel_non_pre_meeting_bot(self):
        bot = self._bot(BotStates.ENDED)
        resp = self.client.post(reverse("bots:cancel-bot", args=[self.project.object_id, bot.object_id]))
        self.assertEqual(resp.status_code, 302)
        bot.refresh_from_db()
        self.assertEqual(bot.state, BotStates.ENDED)  # unchanged

    def test_event_manager_transition_from_scheduled(self):
        bot = self._bot(BotStates.SCHEDULED)
        BotEventManager.create_event(bot, BotEventTypes.BOT_CANCELLED)
        bot.refresh_from_db()
        self.assertEqual(bot.state, BotStates.CANCELLED)

    def test_no_charge_on_cancel(self):
        # BOT_CANCELLED must not incur charges.
        self.assertFalse(BotEventManager.bot_event_type_should_incur_charges(BotEventTypes.BOT_CANCELLED))
