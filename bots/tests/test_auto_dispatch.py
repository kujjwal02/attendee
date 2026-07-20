"""Tests for auto-dispatch policy predicates (self-hosted fork)."""

from types import SimpleNamespace

from django.test import TestCase, override_settings

from bots.auto_dispatch import event_matches_policy, resolve_policy


def _event(platform="google", raw=None, meeting_url="https://meet.google.com/x", name="Sync", is_deleted=False):
    return SimpleNamespace(
        is_deleted=is_deleted,
        meeting_url=meeting_url,
        name=name,
        raw=raw or {},
        calendar=SimpleNamespace(platform=platform),
    )


# Google raw shapes
G_ORGANIZER = {"organizer": {"self": True}, "attendees": [{"self": True, "organizer": True, "responseStatus": "accepted"}]}
G_ACCEPTED = {"organizer": {"email": "boss@x.com"}, "attendees": [{"email": "boss@x.com"}, {"self": True, "responseStatus": "accepted"}]}
G_DECLINED = {"organizer": {"email": "boss@x.com"}, "attendees": [{"self": True, "responseStatus": "declined"}]}
G_INVITED = {"organizer": {"email": "boss@x.com"}, "attendees": [{"self": True, "responseStatus": "needsAction"}]}
G_SOLO = {"organizer": {"self": True}}  # my own event, no attendees

# Microsoft raw shapes
M_ORGANIZER = {"isOrganizer": True, "responseStatus": {"response": "organizer"}}
M_ACCEPTED = {"isOrganizer": False, "responseStatus": {"response": "accepted"}}
M_DECLINED = {"isOrganizer": False, "responseStatus": {"response": "declined"}}
M_INVITED = {"isOrganizer": False, "responseStatus": {"response": "notResponded"}}


class AutoDispatchPolicyTest(TestCase):
    def test_all_requires_url_and_not_deleted(self):
        self.assertTrue(event_matches_policy(_event(raw=G_INVITED), "all", ""))
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED, meeting_url=""), "all", ""))
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED, meeting_url=None), "all", ""))
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED, is_deleted=True), "all", ""))

    def test_participant_default_books_unless_declined(self):
        # organizer, accepted, invited, solo -> part of it
        for raw in (G_ORGANIZER, G_ACCEPTED, G_INVITED, G_SOLO):
            self.assertTrue(event_matches_policy(_event(raw=raw), "participant", ""))
        # declined -> not part of it
        self.assertFalse(event_matches_policy(_event(raw=G_DECLINED), "participant", ""))

    def test_organizer_policy(self):
        self.assertTrue(event_matches_policy(_event(raw=G_ORGANIZER), "organizer", ""))
        self.assertTrue(event_matches_policy(_event(raw=G_SOLO), "organizer", ""))
        self.assertFalse(event_matches_policy(_event(raw=G_ACCEPTED), "organizer", ""))
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED), "organizer", ""))

    def test_accepted_policy(self):
        self.assertTrue(event_matches_policy(_event(raw=G_ACCEPTED), "accepted", ""))
        self.assertTrue(event_matches_policy(_event(raw=G_ORGANIZER), "accepted", ""))  # organizer counts
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED), "accepted", ""))
        self.assertFalse(event_matches_policy(_event(raw=G_DECLINED), "accepted", ""))

    def test_keyword_policy(self):
        self.assertTrue(event_matches_policy(_event(raw=G_INVITED, name="[rec] Standup"), "keyword", "[rec]"))
        self.assertTrue(event_matches_policy(_event(raw=G_INVITED, name="Notetaker please"), "keyword", "notetaker"))
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED, name="Standup"), "keyword", "[rec]"))
        self.assertFalse(event_matches_policy(_event(raw=G_INVITED, name="anything"), "keyword", ""))

    def test_microsoft_shapes(self):
        self.assertTrue(event_matches_policy(_event(platform="microsoft", raw=M_ORGANIZER), "organizer", ""))
        self.assertTrue(event_matches_policy(_event(platform="microsoft", raw=M_ACCEPTED), "accepted", ""))
        self.assertFalse(event_matches_policy(_event(platform="microsoft", raw=M_ACCEPTED), "organizer", ""))
        self.assertFalse(event_matches_policy(_event(platform="microsoft", raw=M_DECLINED), "participant", ""))
        self.assertTrue(event_matches_policy(_event(platform="microsoft", raw=M_INVITED), "participant", ""))


class AutoDispatchResolvePolicyTest(TestCase):
    @override_settings(AUTO_DISPATCH_ENABLED=False, AUTO_DISPATCH_POLICY="participant", AUTO_DISPATCH_KEYWORD="")
    def test_global_defaults(self):
        cal = SimpleNamespace(metadata=None, object_id="cal_x")
        self.assertEqual(resolve_policy(cal), (False, "participant", ""))

    @override_settings(AUTO_DISPATCH_ENABLED=True, AUTO_DISPATCH_POLICY="participant", AUTO_DISPATCH_KEYWORD="")
    def test_per_calendar_override(self):
        cal = SimpleNamespace(metadata={"auto_dispatch": {"enabled": True, "policy": "keyword", "keyword": "[rec]"}}, object_id="cal_x")
        self.assertEqual(resolve_policy(cal), (True, "keyword", "[rec]"))

    @override_settings(AUTO_DISPATCH_ENABLED=True, AUTO_DISPATCH_POLICY="participant", AUTO_DISPATCH_KEYWORD="")
    def test_per_calendar_disable(self):
        cal = SimpleNamespace(metadata={"auto_dispatch": {"enabled": False}}, object_id="cal_x")
        self.assertEqual(resolve_policy(cal), (False, "participant", ""))

    @override_settings(AUTO_DISPATCH_ENABLED=True, AUTO_DISPATCH_POLICY="participant", AUTO_DISPATCH_KEYWORD="")
    def test_invalid_policy_falls_back(self):
        cal = SimpleNamespace(metadata={"auto_dispatch": {"policy": "bogus"}}, object_id="cal_x")
        self.assertEqual(resolve_policy(cal), (True, "participant", ""))
