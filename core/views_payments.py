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

from .models import CustomerProfile
from .utils import ensure_stripe_customer_by_email, create_setup_intent_for_customer
from decouple import config
from .constants import CUSTOMER_SSR
from . import views, views_staff, views_home, veiws_verify, views_payments

User = get_user_model()
stripe.api_key = config('STRIPE_SK')

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


@require_POST
def finalize_signup(request):
    """
    Called by browser after confirmCardSetup succeeds. We receive the setup_intent_id.
    We verify it with Stripe, then create User & CustomerProfile, attach PM as default, log them in.
    """
    import json
    data = json.loads(request.body.decode() or "{}")
    setup_intent_id = (data.get("setup_intent_id") or "").strip()
    if not setup_intent_id:
        return JsonResponse({"ok": False, "error": "Missing setup_intent_id"}, status=400)

    ss = request.session.get(CUSTOMER_SSR)
    if not ss or not ss.get("email_verified") or not ss.get("phone_verified"):
        return JsonResponse({"ok": False, "error": "Signup session missing or incomplete."}, status=400)

    # Retrieve SetupIntent to validate
    try:
        si = stripe.SetupIntent.retrieve(setup_intent_id)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Stripe error: {e}"}, status=400)

    if si.get("status") != "succeeded":
        return JsonResponse({"ok": False, "error": "Card was not saved."}, status=400)

    pm_id = si.get("payment_method")
    customer_id = si.get("customer")
    if not (pm_id and customer_id):
        return JsonResponse({"ok": False, "error": "Payment method not found on SetupIntent."}, status=400)

    # Create Django user & CustomerProfile NOW (first time we touch DB)
    email = ss["email"]
    first_name = ss.get("first_name") or ""
    last_name  = ss.get("last_name") or ""
    phone      = ss.get("phone") or ""
    password1  = ss.get("password1")  # present for new users

    user = User.objects.filter(email=email).first()
    if not user:
        user = User.objects.create_user(
            username=email,
            email=email,
            password=password1,
            first_name=first_name,
            last_name=last_name,
        )
    user.is_active = True
    user.save(update_fields=["is_active"])

    cp, _ = CustomerProfile.objects.get_or_create(user=user)
    cp.phone = phone
    if hasattr(cp, "phone_verified"): cp.phone_verified = True
    if hasattr(cp, "email_verified"): cp.email_verified  = True
    cp.stripe_customer_id = customer_id
    cp.default_payment_method = pm_id
    cp.save()

    # Make PM default on the Stripe customer
    try:
        stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": pm_id})
    except Exception:
        pass

    # Clear the pending session
    request.session.pop(CUSTOMER_SSR, None)
    request.session.modified = True

    # Optionally log them in
    login(request, user)

    return JsonResponse({"ok": True, "redirect": "/profile"})

