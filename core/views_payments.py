# core/views_payments.py
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth import get_user_model
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

import stripe
from allauth.socialaccount.models import SocialAccount
from .models import CustomerProfile, Member
from .utils import ensure_stripe_customer_by_email, create_setup_intent_for_customer
from decouple import config
from .constants import CUSTOMER_SSR
from . import views, views_staff, views_home, veiws_verify, views_payments
from django.contrib.auth.hashers import make_password
import random
import re


User = get_user_model()
stripe.api_key = config('STRIPE_SK')

def _names_from_google(user):
    """
    Try to get first/last name from the linked Google SocialAccount.
    Returns (first, last), either may be '' if not available.
    """
    first = (getattr(user, "first_name", "") or "").strip()
    last  = (getattr(user, "last_name", "") or "").strip()

    if first and last:
        return first, last

    sa = SocialAccount.objects.filter(user=user, provider="google").first()
    if not sa:
        return first, last

    data = (sa.extra_data or {})
    first2 = (data.get("given_name") or "").strip()
    last2  = (data.get("family_name") or "").strip()
    if not (first2 or last2):
        # Fallback: split "name"
        full = (data.get("name") or "").strip()
        if full:
            parts = full.split()
            if len(parts) >= 2:
                first2, last2 = parts[0], " ".join(parts[1:])
            else:
                first2 = full

    return first or first2, last or last2


@ensure_csrf_cookie
def add_card(request):
    """
    Shows Stripe Elements for a *pending* signup.
    Requires session bundle with stage='need_card' set by verify_email_otp.
    """
    ss = request.session.get(CUSTOMER_SSR)
    #print(ss)  # uncomment for debugging

    # If session missing or wrong stage, send them back to customer signup.
    # (Use the actual path if you don't have a named URL.)
    if not ss or ss.get("stage") != "need_card" or not ss.get("email"):
        return redirect("/customer/signup")  # avoids NoReverseMatch

    email = ss["email"].strip().lower()

    # Create/fetch Stripe Customer for this pending signup
    customer_id = ss.get("stripe_customer_id_pending")
    if not customer_id:
        customer_id = ensure_stripe_customer_by_email(
            email,
            metadata={"signup": "pending"}
        )
        ss["stripe_customer_id_pending"] = customer_id
        request.session[CUSTOMER_SSR] = ss
        request.session.modified = True

    # Create SetupIntent for saving a card
    si = create_setup_intent_for_customer(customer_id)

    return render(request, "core/add_card.html", {
        "pk": config('STRIPE_PK'),
        "client_secret": si.client_secret,
        "next": request.GET.get("next") or "/profile",
    })

@ensure_csrf_cookie
def set_pin(request):
    """
    Show a small 2-field PIN form right after card save.
    Stores the setup_intent_id temporarily in session.
    """
    ss = request.session.get(CUSTOMER_SSR)
    if not ss or not ss.get("email_verified") or not ss.get("phone_verified"):
        return redirect("/customer/signup")

    si = (request.GET.get("si") or "").strip()
    if not si:
        return redirect("/customer/signup")

    # stash SI in session so POST doesn't rely on querystring
    ss["pending_setup_intent_id"] = si
    request.session[CUSTOMER_SSR] = ss
    request.session.modified = True

    return render(request, "core/set_pin.html", {
        "next": request.GET.get("next") or "/profile",
    })


@require_POST
def save_pin_finalize(request):
    """
    Validate the PIN, then finalize signup using the previously-saved setup_intent_id.
    Also fills in missing names from Google OAuth, and assigns a unique member number.
    """
    import json
    data = json.loads(request.body.decode() or "{}")
    pin1 = (data.get("pin1") or "").strip()
    pin2 = (data.get("pin2") or "").strip()
    next_url = (data.get("next") or "/profile").strip()

    if not (pin1.isdigit() and len(pin1) == 4 and pin1 == pin2):
        return JsonResponse({"ok": False, "error": "Enter matching 4-digit PIN."}, status=400)

    ss = request.session.get(CUSTOMER_SSR)
    if not ss or not ss.get("email_verified") or not ss.get("phone_verified"):
        return JsonResponse({"ok": False, "error": "Signup session missing or incomplete."}, status=400)

    setup_intent_id = ss.get("pending_setup_intent_id")
    if not setup_intent_id:
        return JsonResponse({"ok": False, "error": "Missing saved card reference."}, status=400)

    # Validate SetupIntent
    try:
        si = stripe.SetupIntent.retrieve(setup_intent_id)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Stripe error: {e}"}, status=400)
    if si.get("status") != "succeeded":
        return JsonResponse({"ok": False, "error": "Card was not saved."}, status=400)

    pm_id = si.get("payment_method")
    customer_id = si.get("customer")
    if not (pm_id and customer_id):
        return JsonResponse({"ok": False, "error": "Payment method missing from SetupIntent."}, status=400)

    # Create (or fetch) user
    email      = ss["email"]
    first_name = (ss.get("first_name") or "").strip()
    last_name  = (ss.get("last_name") or "").strip()
    phone      = (ss.get("phone") or "").strip()
    password1  = ss.get("password1")  # may be None for OAuth

    user = User.objects.filter(email=email).first()
    if not user:
        user = User.objects.create_user(
            username=email, email=email,
            password=password1 if password1 else None
        )
        if not password1:
            user.set_unusable_password()

    # If names are blank (common for OAuth), pull from Google social account
    if not (first_name and last_name):
        g_first, g_last = _names_from_google(user)
        first_name = first_name or g_first or ""
        last_name  = last_name  or g_last  or ""

    # Persist names on the user if we have them
    changed = False
    if first_name and user.first_name != first_name:
        user.first_name = first_name; changed = True
    if last_name and user.last_name != last_name:
        user.last_name = last_name; changed = True

    user.is_active = True
    if changed:
        user.save(update_fields=["first_name", "last_name", "is_active"])
    else:
        user.save(update_fields=["is_active"])

    # CustomerProfile + PIN
    cp, _ = CustomerProfile.objects.get_or_create(user=user)
    if phone:
        cp.phone = phone
    if hasattr(cp, "phone_verified"):
        cp.phone_verified = True
    if hasattr(cp, "email_verified"):
        cp.email_verified  = True
    cp.stripe_customer_id = customer_id
    cp.default_payment_method = pm_id
    cp.pin_hash = make_password(pin1)
    cp.save()

    # Make PM default at Stripe
    try:
        stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": pm_id})
    except Exception:
        pass

    # --- Member number (uses last name; falls back gracefully) ---
    # Prefix = first 4 letters of last name or 'XXXX'
    ln = (user.last_name or "").strip().upper()
    prefix = (ln[:4] if ln else "XXXX").ljust(4, "X")

    # Last-4 = digits from phone, else random
    digits = re.sub(r"\D", "", phone or "")
    last4 = (digits[-4:] if len(digits) >= 4 else f"{random.randint(0,9999):04d}")

    base = f"{prefix}{last4}"

    from core.models import Member
    number = base
    tries = 0
    while Member.objects.filter(number=number).exists() and tries < 100:
        number = f"{prefix}{random.randint(0,9999):04d}"
        tries += 1

    member, created = Member.objects.get_or_create(
        customer=cp,
        defaults={"number": number, "last_name": user.last_name or ""},
    )
    if not created:
        # If it exists but is missing fields, fill them
        updated = False
        if not member.number:
            member.number = number; updated = True
        if not member.last_name and user.last_name:
            member.last_name = user.last_name; updated = True
        if updated:
            member.save()

    # cleanup session
    for k in ("pending_setup_intent_id",):
        ss.pop(k, None)
    request.session[CUSTOMER_SSR] = ss
    request.session.pop(CUSTOMER_SSR, None)
    request.session.modified = True

    login(request, user)
    return JsonResponse({"ok": True, "redirect": next_url})



