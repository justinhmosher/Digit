# views_resetpin.py
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.shortcuts import render, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .utils import send_customer_pin_reset_email
from .models import CustomerProfile, PinResetToken
from django.contrib import messages
from django.contrib.auth.hashers import make_password



# =========================
# Helpers
# =========================

def _make_raw_token_and_hash() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash

def _hash_pin(pin: str) -> str:
    """HMAC-SHA256 with SECRET_KEY for short numeric PINs."""
    return hmac.new(settings.SECRET_KEY.encode(), pin.encode(), hashlib.sha256).hexdigest()


# =========================
# Token creation + emailing
# =========================

def create_customer_pin_reset(customer_profile: CustomerProfile, request, ttl_minutes: int = 60):
    """
    Create single-use token (store HASH ONLY), return (reset_url, expires_at, token_obj).
    Ensures no other active tokens remain for this customer.
    """
    # Invalidate any previously unused tokens for this customer (optional but safer)
    PinResetToken.objects.filter(customer=customer_profile, used=False, expires_at__gt=timezone.now()).update(used=True)

    raw, token_hash = _make_raw_token_and_hash()
    expires_at = timezone.now() + timedelta(minutes=ttl_minutes)

    token_obj = PinResetToken.objects.create(
        customer=customer_profile,
        token_hash=token_hash,
        expires_at=expires_at,
    )

    reset_path = reverse("core:reset_pin_confirm", args=[raw])
    reset_url = request.build_absolute_uri(reset_path)
    return reset_url, expires_at, token_obj


# =========================
# View: consume token + set new PIN
# =========================

@require_http_methods(["GET", "POST"])
def reset_pin_confirm(request, token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    try:
        prt = PinResetToken.objects.select_related("customer", "customer__user").get(token_hash=token_hash)
    except PinResetToken.DoesNotExist:
        messages.error(request, "Invalid or expired link.")
        return redirect("login")

    if not (not prt.used and timezone.now() < prt.expires_at):
        messages.error(request, "This reset link has expired or already been used.")
        return redirect("login")

    if request.method == "POST":
        new_pin = (request.POST.get("pin") or "").strip()
        confirm = (request.POST.get("pin_confirm") or "").strip()

        # Server-side validation (client JS enforces too, but never trust client)
        if not (new_pin.isdigit() and len(new_pin) == 4):
            messages.error(request, "PIN must be exactly 4 digits.")
            return render(request, "core/reset_pin_confirm.html", {"customer": prt.customer})

        if new_pin != confirm:
            messages.error(request, "PINs do not match.")
            return render(request, "core/reset_pin_confirm.html", {"customer": prt.customer})

        # Save hashed PIN on the CustomerProfile
        prt.customer.pin_hash = make_password(new_pin)
        prt.customer.save(update_fields=["pin_hash"])

        # Consume token
        prt.used = True
        prt.save(update_fields=["used"])

        messages.success(request, "Your PIN has been reset.")
        return redirect("core:profile")

    return render(request, "core/reset_pin_confirm.html", {"customer": prt.customer})


