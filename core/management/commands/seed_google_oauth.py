# core/management/commands/seed_google_oauth.py
from django.core.management.base import BaseCommand
from django.contrib.sites.models import Site
from allauth.socialaccount.models import SocialApp
from decouple import config

class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        cid = config("GOOGLE_CLIENT_ID", default=None)
        secret = config("GOOGLE_CLIENT_SECRET", default=None)
        if not cid or not secret:
            self.stderr.write("Missing GOOGLE_CLIENT_ID/SECRET")
            return
        app, created = SocialApp.objects.get_or_create(provider="google", defaults={
            "name": "Google", "client_id": cid, "secret": secret
        })
        if not created:
            app.client_id, app.secret = cid, secret
            app.save()
        app.sites.add(Site.objects.get_current())
        self.stdout.write("Google SocialApp configured.")
