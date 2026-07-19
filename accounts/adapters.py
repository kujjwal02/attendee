import logging

import requests
from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.urls import reverse

logger = logging.getLogger(__name__)


def signup_allowed_for_email(email: str) -> bool:
    """Whether a NEW account may be created for this email.

    Controlled by two env-driven settings (see settings/base.py):
      - DISABLE_SIGNUP: if set, no new signups at all (hard block).
      - SIGNUP_ALLOWED_EMAILS: if non-empty, only these exact emails may sign up;
        if empty/unset, signups are open (default upstream behavior).

    Only consulted for NEW signups — existing users (and already-linked social
    accounts) are unaffected, so a misconfigured list cannot lock anyone out.
    """
    if getattr(settings, "DISABLE_SIGNUP", False):
        return False
    allowed = getattr(settings, "SIGNUP_ALLOWED_EMAILS", None) or []
    if not allowed:
        return True
    return bool(email) and email.strip().lower() in allowed


def validate_email_with_mailgun(email: str) -> None:
    if settings.BYPASS_MAILGUN_VALIDATION_SUBSTRING and settings.BYPASS_MAILGUN_VALIDATION_SUBSTRING in email:
        return

    try:
        response = requests.post(
            "https://api.mailgun.net/v4/address/validate",
            auth=("api", settings.MAILGUN_VALIDATION_API_KEY),
            data={"address": email},
            params={"provider_lookup": "true"},
            timeout=(2, 4),  # connect timeout, read timeout,
        )
        response.raise_for_status()
        validation = response.json()
    except Exception as exc:
        logger.warning(
            f"Mailgun email validation failed for email {email}",
            exc_info=exc,
        )
        return

    if validation.get("is_disposable_address"):
        raise ValidationError("Please use a permanent email address.")

    result = validation.get("result")

    if result in {"undeliverable", "do_not_send"}:
        raise ValidationError("This email address does not appear to be valid.")


class StandardAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Hard on/off switch; the per-email allow-list is enforced in clean_email
        # so the user gets a clear "not allowed" message rather than a hidden form.
        return not getattr(settings, "DISABLE_SIGNUP", False)

    def clean_email(self, email: str) -> str:
        email = super().clean_email(email)

        if settings.MAILGUN_VALIDATION_API_KEY:
            validate_email_with_mailgun(email)

        if not signup_allowed_for_email(email):
            raise ValidationError("Sign-ups are restricted on this instance. Contact the administrator for access.")

        return email

    def get_email_verification_redirect_url(self, email_address):
        user = email_address.user
        if getattr(user, "invited_by", None):
            return reverse("account_set_password")
        return super().get_email_verification_redirect_url(email_address)

    def confirm_email(self, request, email_address):
        """
        Marks the given email address as confirmed on the db and logs in the user
        if they were invited by someone else.
        """
        # Call the parent method to handle the confirmation
        confirm_email_response = super().confirm_email(request, email_address)

        # Log in the user if they were invited and not already authenticated
        # Even though we set ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION to True, django will not log the user
        # in because they are coming from a different machine then the one that sent the email.
        user = email_address.user
        if user.invited_by and not request.user.is_authenticated:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")

        return confirm_email_response


class NoNewUsersAccountAdapter(StandardAccountAdapter):
    def is_open_for_signup(self, request):
        return False


class RestrictedSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Gates NEW social (e.g. Google) signups by the same rules as email signups.

    allauth checks the ACCOUNT adapter's is_open_for_signup for email/password
    signups and the SOCIALACCOUNT adapter's for social ones — they are separate, so
    the account adapter alone does not stop Google from creating new users. This
    enforces DISABLE_SIGNUP + SIGNUP_ALLOWED_EMAILS for the social path too.
    Existing users (already-linked social accounts) are unaffected; is_open_for_signup
    is only consulted when a social login does not match an existing user.
    """

    def is_open_for_signup(self, request, sociallogin):
        email = getattr(sociallogin.user, "email", None)
        if not email and getattr(sociallogin, "email_addresses", None):
            email = sociallogin.email_addresses[0].email
        return signup_allowed_for_email(email)
