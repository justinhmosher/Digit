# core/adapters.py
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from allauth.exceptions import ImmediateHttpResponse
from django.shortcuts import redirect
from .models import CustomerProfile

class GoogleGateAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin: SocialLogin):
        # Who is flowing through? (set by your /owner/google-start/ or default to 'customer')
        gate_role = request.session.get("auth_role", "customer")

        # If this is an existing social account and already allowed, skip gating.
        if sociallogin.is_existing:
            user = sociallogin.user
            if gate_role == "owner":
                # owner: if they already have an owner profile, let them in
                if getattr(user, "ownerprofile", None):
                    return
            else:
                # customer: if phone verified, let them straight in
                prof = getattr(user, "customerprofile", None)
                if prof and getattr(prof, "phone_verified", False):
                    return
            # else fall through to gate

        # Stash & gate based on role
        request.session["pending_sociallogin"] = sociallogin.serialize()
        request.session["pending_email"] = (sociallogin.user.email or "").lower()
        request.session.modified = True

        if gate_role == "owner":
            # send owners to their phone step for owner flow
            raise ImmediateHttpResponse(redirect("core:oauth_owner_phone_page"))
        else:
            # send customers to customer phone step
            raise ImmediateHttpResponse(redirect("core:oauth_phone_page"))

