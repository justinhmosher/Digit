# core/views_restaurants.py
from __future__ import annotations
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
)
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

import stripe

from core.models import RestaurantProfile, OwnerProfile
from decouple import config

stripe.api_key = config("STRIPE_SK")


# Small helper to find the current restaurant being onboarded
def _get_current_restaurant(request: HttpRequest) -> RestaurantProfile | None:
    rid = request.session.get("current_restaurant_id")
    if not rid:
        return None
    return RestaurantProfile.objects.filter(id=rid).first()


def _link_owner(rp: RestaurantProfile, owner: OwnerProfile):
    """
    Safely link owner -> restaurant whether owners is a plain M2M or a M2M through=Ownership.
    Adjust model/field names if your through model differs.
    """
    # Plain M2M?
    try:
        rp.owners.add(owner)
        return
    except Exception:
        pass

    # M2M with 'through'
    try:
        from core.models import Ownership  # adjust import path if needed
        Ownership.objects.get_or_create(restaurant=rp, owner=owner)
    except Exception as e:
        raise RuntimeError(f"Could not link owner to restaurant: {e}")

@login_required
@require_http_methods(["GET", "POST"])
def restaurant_onboard(request: HttpRequest) -> HttpResponse:
    owner, _ = OwnerProfile.objects.get_or_create(user=request.user)

    if request.method == "GET":
        legacy_profile = getattr(request.user, "restaurant_profile", None)
        current_id = request.session.get("current_restaurant_id")
        current_profile = RestaurantProfile.objects.filter(id=current_id).first() if current_id else None
        profile = current_profile or legacy_profile
        return render(
            request,
            "core/restaurant_onboard.html",
            {"owner": owner, "profile": profile, "GOOGLE_MAPS_API_KEY": config("GOOGLE_MAPS_API")},
        )

    # ----- POST JSON -----
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)

    # DEBUG: show payload in console so we can see what’s coming in
    print("[onboard] POST data:", data)

    legal_name = (data.get("legal_name") or "").strip()
    email      = (data.get("email") or "").strip().lower()
    dba_name   = (data.get("dba_name") or "").strip()
    phone      = (data.get("phone") or "").strip()

    addr_line1 = (data.get("addr_line1") or "").strip()
    addr_line2 = (data.get("addr_line2") or "").strip()
    city       = (data.get("city") or "").strip()
    state      = (data.get("state") or "").strip()
    postal     = (data.get("postal") or "").strip()

    omnivore_location_id = (data.get("omnivore_location_id") or "").strip()

    if not legal_name or not email:
        return JsonResponse({"ok": False, "error": "Legal name and email are required."}, status=400)
    if not addr_line1 or not city or not state or not postal:
        return JsonResponse({"ok": False, "error": "Complete address is required."}, status=400)

    try:
        # Create/update profile (support legacy one-to-one if present)
        if hasattr(RestaurantProfile, "user"):
            rp, _ = RestaurantProfile.objects.get_or_create(user=request.user)
            rp.legal_name = legal_name
            rp.dba_name   = dba_name
            rp.email      = email
            rp.phone      = phone
            rp.addr_line1 = addr_line1
            rp.addr_line2 = addr_line2
            rp.city       = city
            rp.state      = state
            rp.postal     = postal
            rp.omnivore_location_id = omnivore_location_id
            rp.is_active  = True
            rp.save()
        else:
            rp = RestaurantProfile.objects.create(
                legal_name=legal_name,
                dba_name=dba_name,
                email=email,
                phone=phone,
                addr_line1=addr_line1,
                addr_line2=addr_line2,
                city=city,
                state=state,
                postal=postal,
                omnivore_location_id=omnivore_location_id,
                is_active=True,
            )

        # Link ownership robustly
        _link_owner(rp, owner)

        # Remember selection
        request.session["current_restaurant_id"] = rp.id
        request.session.modified = True

    except Exception as e:
        # Show the exception detail in JSON so you can diagnose quickly
        return JsonResponse({"ok": False, "error": "onboard_failed", "detail": str(e)}, status=500)

    return JsonResponse({"ok": True, "restaurant_id": rp.id, "redirect": reverse("core:restaurant_add_card_start")})

@login_required
@require_http_methods(["GET"])
def restaurant_add_card_start(request: HttpRequest) -> HttpResponse:
    """
    Render a page where the restaurant owner can add a card.
    - Ensures the RestaurantProfile has a Stripe Customer.
    - Creates a SetupIntent and passes client_secret + publishable key to the template.
    """
    rp = _get_current_restaurant(request)
    if not rp:
        return HttpResponseBadRequest("No current restaurant in session.")

    # Ensure Stripe Customer exists for this restaurant
    if not rp.stripe_customer_id:
        cust = stripe.Customer.create(
            name=rp.dba_name or rp.legal_name,
            email=rp.email or None,
            metadata={"restaurant_id": str(rp.id)},
        )
        rp.stripe_customer_id = cust.id
        rp.save(update_fields=["stripe_customer_id"])

    # Create SetupIntent to collect/attach a card to the customer
    si = stripe.SetupIntent.create(
        customer=rp.stripe_customer_id,
        payment_method_types=["card"],
        usage="off_session",
    )

    context = {
        "pk": config("STRIPE_PK"),
        "client_secret": si.client_secret,
        # where to send the user after confirm succeeds:
        "next": reverse("core:owner_dashboard"),
        "restaurant": rp,
    }
    return render(request, "core/restaurant_add_card.html", context)


@login_required
@require_http_methods(["GET"])
def restaurant_set_card(request: HttpRequest) -> HttpResponse:
    """
    Finalize card saving after Stripe.js confirmCardSetup.
    Query param: ?si=seti_xxx&next=/wherever
    - Retrieves the SetupIntent
    - Persists default PaymentMethod to RestaurantProfile
    - Sets the Customer's default payment method for invoices/PMs
    """
    rp = _get_current_restaurant(request)
    if not rp:
        return HttpResponseBadRequest("No current restaurant in session.")

    si_id = request.GET.get("si", "")
    nxt = request.GET.get("next") or reverse("core:owner_dashboard")
    if not si_id:
        return HttpResponseBadRequest("Missing SetupIntent id.")

    try:
        si = stripe.SetupIntent.retrieve(si_id)
    except Exception as e:
        return HttpResponseBadRequest(f"Could not retrieve SetupIntent: {e}")

    if si.get("status") != "succeeded":
        return HttpResponseBadRequest("SetupIntent not succeeded.")

    pm_id = si.get("payment_method")
    if not pm_id:
        return HttpResponseBadRequest("No payment method on SetupIntent.")

    # Attach PM to the Customer if not already
    try:
        stripe.PaymentMethod.attach(
            pm_id,
            customer=rp.stripe_customer_id,
        )
    except stripe.error.InvalidRequestError:
        # It's OK if it's already attached
        pass

    # Make it default for this customer (useful for future billing)
    stripe.Customer.modify(
        rp.stripe_customer_id,
        invoice_settings={"default_payment_method": pm_id},
    )

    # Persist on the restaurant profile
    rp.stripe_default_pm_id = pm_id
    rp.save(update_fields=["stripe_default_pm_id"])

    return redirect(nxt)
