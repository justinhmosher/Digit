# core/adapters.py
from django.shortcuts import redirect
from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from allauth.account.utils import perform_login
from django.contrib.auth import get_user_model

from .models import OwnerProfile, RestaurantProfile, CustomerProfile  # adjust paths

User = get_user_model()

class GoogleGateAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin: SocialLogin):
        # who is flowing through? (set by your /owner/google-start/ or default to 'customer')
        gate_role = request.session.get("auth_role", "customer")

        # helper: stash the sociallogin so OTP step can restore/deserialize it
        def stash_and_gate(to_url_name: str):
            request.session["pending_sociallogin"] = sociallogin.serialize()
            request.session["pending_email"] = (sociallogin.user.email or "").lower()
            request.session["auth_role"] = gate_role
            request.session.modified = True
            raise ImmediateHttpResponse(redirect(to_url_name))

        # resolve the "local" user if this social account already exists
        user = None
        if sociallogin.is_existing:
            # already linked: this is the local Django user
            user = sociallogin.account.user
        else:
            # new sociallogin; try to match an existing user by email
            email = (sociallogin.user.email or "").lower()
            if email:
                user = User.objects.filter(email__iexact=email).first()

        # ===== OWNER FLOW =====
        if gate_role == "owner":
            # If we don't have a local user yet, we must OTP to create/attach everything.
            if not user:
                return stash_and_gate("core:oauth_owner_phone_page")

            # Look up owner profile
            op = getattr(user, "ownerprofile", None) or OwnerProfile.objects.filter(user=user).first()
            if not op:
                # No owner profile yet → OTP flow will create OwnerProfile and mark phone
                return stash_and_gate("core:oauth_owner_phone_page")

            # Has owner profile; check phone verification
            phone_ok = getattr(op, "phone_verified", False)

            if not phone_ok:
                # Need phone verification
                return stash_and_gate("core:oauth_owner_phone_page")

            # Phone verified: do they have at least one restaurant?
            has_restaurant = RestaurantProfile.objects.filter(owners=op).exists()
            # If you have a "verified" flag on restaurant, replace with:
            # has_restaurant = RestaurantProfile.objects.filter(owners=op, status="verified").exists()

            # If the social account isn't linked yet, connect it to this user
            if not sociallogin.is_existing:
                sociallogin.connect(request, user)

            # Log them in now so next view sees request.user
            perform_login(request, user, email_verification=None)

            if has_restaurant:
                # Fully ready → owner dashboard / post_login
                raise ImmediateHttpResponse(redirect("core:post_login_owner"))
            else:
                # Phone OK but needs onboarding a restaurant
                raise ImmediateHttpResponse(redirect("core:restaurant_onboard"))

        # ===== CUSTOMER FLOW (unchanged from your original, with a small safety tweak) =====
        # For customers we allow straight-through if they already have a verified customer profile.
        if sociallogin.is_existing:
            u = sociallogin.account.user
            prof = getattr(u, "customerprofile", None)
            if prof and getattr(prof, "phone_verified", False):
                return  # allow default allauth completion

        # Otherwise, stash and gate customers to their phone page
        return stash_and_gate("core:oauth_phone_page")

