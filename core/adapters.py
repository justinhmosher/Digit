# core/adapters.py
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from django.http import HttpResponse
from django.shortcuts import redirect
from allauth.exceptions import ImmediateHttpResponse
from django.contrib.auth import authenticate, login, logout

class GoogleGateAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin: SocialLogin):
        # If the social account already exists, let allauth continue normally.
        #logout(request) 
        request.session['__gate_hit'] = True

        # ALWAYS gate users who aren't authenticated yet,
        # even if the social account already exists.
        # Always stash and force phone gate
        request.session["pending_sociallogin"] = sociallogin.serialize()
        request.session["pending_email"] = (sociallogin.user.email or "").lower()
        request.session.modified = True
        request.session.save()

        raise ImmediateHttpResponse(redirect("core:oauth_phone_page"))



