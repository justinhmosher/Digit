# core/adapters.py
from django.shortcuts import redirect
from django.urls import reverse
from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin, SocialAccount
from allauth.account.utils import perform_login
from django.contrib.auth import get_user_model
from urllib.parse import quote

from .models import OwnerProfile, RestaurantProfile, CustomerProfile, ManagerProfile, StaffProfile  # + ManagerProfile

User = get_user_model()

class GoogleGateAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin: SocialLogin):
        # Set this before sending users to Google from your manager/owner/customer start pages
        gate_role = request.session.get("auth_role", "customer")

        def stash_and_gate(to_url_name: str):
            request.session["pending_sociallogin"] = sociallogin.serialize()
            request.session["pending_email"] = (sociallogin.user.email or "").lower()
            request.session["auth_role"] = gate_role
            request.session.modified = True
            raise ImmediateHttpResponse(redirect(to_url_name))

        email = (sociallogin.user.email or "").lower()
        # Resolve the local user if possible
        user = None
        if sociallogin.is_existing:
            user = sociallogin.account.user
        else:
            email = (sociallogin.user.email or "").lower()
            if email:
                user = User.objects.filter(email__iexact=email).first()

        # ---------------------------------------------------------

        # ===== MANAGER FLOW =====
        if gate_role == "manager":
            if user and not sociallogin.is_existing:
                if user.has_usable_password() and not SocialAccount.objects.filter(
                    user=user, provider=sociallogin.account.provider
                ).exists():
                    login_url = reverse("core:restaurant_signin")
                    # carry context so the UI can show a friendly message
                    login_url += f"?reason=password-account&email={quote(email)}"
                    raise ImmediateHttpResponse(redirect(login_url))
            # Must be an existing user with a ManagerProfile
            if not user:
                # No local user for this email → not invited
                signin_url = reverse("core:restaurant_signin") + "?error=manager_invite_required"
                raise ImmediateHttpResponse(redirect(signin_url))

            mp = getattr(user, "managerprofile", None) or ManagerProfile.objects.filter(user=user).first()
            if not mp:
                # Not a manager on this account → bounce to restaurant sign-in with error
                signin_url = reverse("core:restaurant_signin") + "?error=manager_invite_required"
                raise ImmediateHttpResponse(redirect(signin_url))

            # Good to go: ensure the social account is linked, then log in and redirect
            if not sociallogin.is_existing:
                sociallogin.connect(request, user)
            perform_login(request, user, email_verification=None)
            raise ImmediateHttpResponse(redirect("core:manager_dashboard"))

            # ===== STAFF FLOW =====
        if gate_role == "staff":
            if user and not sociallogin.is_existing:
                if user.has_usable_password() and not SocialAccount.objects.filter(
                    user=user, provider=sociallogin.account.provider
                ).exists():
                    login_url = reverse("core:restaurant_signin")
                    # carry context so the UI can show a friendly message
                    login_url += f"?reason=password-account&email={quote(email)}"
                    raise ImmediateHttpResponse(redirect(login_url))
            # Must be an existing user with a StaffProfile
            if not user:
                # No local user for this email → not invited
                signin_url = reverse("core:restaurant_signin") + "?error=staff_invite_required"
                raise ImmediateHttpResponse(redirect(signin_url))

            mp = getattr(user, "staffprofile", None) or StaffProfile.objects.filter(user=user).first()
            if not mp:
                # Not a staff on this account → bounce to restaurant sign-in with error
                signin_url = reverse("core:restaurant_signin") + "?error=staff_invite_required"
                raise ImmediateHttpResponse(redirect(signin_url))

            # Good to go: ensure the social account is linked, then log in and redirect
            if not sociallogin.is_existing:
                sociallogin.connect(request, user)
            perform_login(request, user, email_verification=None)
            raise ImmediateHttpResponse(redirect("core:staff_console"))

        # ===== OWNER FLOW =====
        if gate_role == "owner":
            if user and not sociallogin.is_existing:
                if user.has_usable_password() and not SocialAccount.objects.filter(
                    user=user, provider=sociallogin.account.provider
                ).exists():
                    login_url = reverse("core:restaurant_signin")
                    # carry context so the UI can show a friendly message
                    login_url += f"?reason=password-account&email={quote(email)}"
                    raise ImmediateHttpResponse(redirect(login_url))
            if not user:
                return stash_and_gate("core:oauth_owner_phone_page")

            op = getattr(user, "ownerprofile", None) or OwnerProfile.objects.filter(user=user).first()
            if not op:
                return stash_and_gate("core:oauth_owner_phone_page")

            if not getattr(op, "phone_verified", False):
                return stash_and_gate("core:oauth_owner_phone_page")

            has_restaurant = RestaurantProfile.objects.filter(owners=op).exists()
            if not sociallogin.is_existing:
                sociallogin.connect(request, user)
            perform_login(request, user, email_verification=None)

            if has_restaurant:
                raise ImmediateHttpResponse(redirect("core:post_login_owner"))
            else:
                raise ImmediateHttpResponse(redirect("core:connect_onboard_start"))

        if user and not sociallogin.is_existing:
                if user.has_usable_password() and not SocialAccount.objects.filter(
                    user=user, provider=sociallogin.account.provider
                ).exists():
                    login_url = reverse("core:signin")
                    # carry context so the UI can show a friendly message
                    login_url += f"?reason=password-account&email={quote(email)}"
                    raise ImmediateHttpResponse(redirect(login_url))
        # ===== CUSTOMER FLOW =====
        if sociallogin.is_existing:
            u = sociallogin.account.user
            prof = getattr(u, "customerprofile", None)
            if prof and getattr(prof, "phone_verified", False):
                return  ImmediateHttpResponse(redirect("core:profile"))
            else:
                return ImmediateHttpResponse(redirect("core:oauth_verify_existing"))
        # Default: send customers to phone OTP
        return stash_and_gate("core:oauth_phone_page")
