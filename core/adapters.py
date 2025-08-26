# core/adapters.py
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from allauth.exceptions import ImmediateHttpResponse
from django.shortcuts import redirect
from .models import CustomerProfile

class GoogleGateAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin: SocialLogin):
        if sociallogin.is_existing:
            user = sociallogin.user
            profile = getattr(user, "customerprofile", None)
            if profile and getattr(profile, "phone_verified", False):
                return  # allow normal allauth login, no OTP
            # phone missing/unverified -> gate
        # first-time or unverified -> stash & gate
        request.session["pending_sociallogin"] = sociallogin.serialize()
        request.session["pending_email"] = (sociallogin.user.email or "").lower()
        request.session.modified = True
        if gate_role == "owner":
            raise ImmediateHttpResponse(redirect("core:oauth_owner_phone_page"))
        else:
            raise ImmediateHttpResponse(redirect("core:oauth_phone_page"))
        raise ImmediateHttpResponse(redirect("core:oauth_phone_page"))

