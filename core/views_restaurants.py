# core/views_restaurants.py
from __future__ import annotations
import json
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from decouple import config
import stripe

from core.models import RestaurantProfile, OwnerProfile

stripe.api_key = config("STRIPE_SK")


def _abs(request: HttpRequest, named_url: str) -> str:
    """Build absolute URL from a named route."""
    return request.build_absolute_uri(reverse(named_url))

def _get_current_restaurant(request: HttpRequest) -> RestaurantProfile | None: 
    rid = request.session.get("current_restaurant_id")
    print(rid)
    return RestaurantProfile.objects.filter(id=rid).first() if rid else None

def _link_owner(rp: RestaurantProfile, owner: OwnerProfile) -> None:
    """
    Link owner -> restaurant whether owners is a plain M2M or M2M through=Ownership.
    """
    # Plain M2M?
    try:
        rp.owners.add(owner)
        return
    except Exception:
        pass

    # M2M with through table
    try:
        from core.models import Ownership  # adjust import path if needed
        Ownership.objects.get_or_create(restaurant=rp, owner=owner)
    except Exception as e:
        raise RuntimeError(f"Could not link owner to restaurant: {e}")
# --- helper: ensure there is a current restaurant, creating a placeholder if needed ---
def _ensure_current_restaurant(request: HttpRequest) -> RestaurantProfile:
    rp = _get_current_restaurant(request)
    if rp:
        return rp

    # Create a minimal placeholder record for this owner
    owner, _ = OwnerProfile.objects.get_or_create(user=request.user)

    # Nice default display name for first-time owners
    fallback_name = (request.user.get_full_name() or (request.user.email or "New") ).split("@")[0]
    legal = f"{fallback_name} Restaurant"

    rp = RestaurantProfile.objects.create(
        legal_name=legal,
        dba_name="",
        email=request.user.email or "",
        phone="",
        addr_line1="",
        addr_line2="",
        city="",
        state="",
        postal="",
        omnivore_location_id="",
        is_active=True,
    )
    # Link ownership and remember in session
    _link_owner(rp, owner)
    request.session["current_restaurant_id"] = rp.id
    request.session.modified = True
    return rp


@login_required
@require_http_methods(["GET"])
def connect_onboard_start(request: HttpRequest):
    """
    Ensure a Restaurant exists and has a Connect Express account,
    then send owner to Stripe-hosted onboarding.
    """
    rp = _ensure_current_restaurant(request)

    # Create account if missing
    if not getattr(rp, "stripe_account_id", ""):
        acct = stripe.Account.create(
            type="express",
            country="US",
            email=rp.email or request.user.email or None,
            business_type="company",  # or "individual" if that suits your sellers
            business_profile={"name": (rp.dba_name or rp.legal_name)[:255]},
            capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
            metadata={"restaurant_id": str(rp.id)},
        )
        rp.stripe_account_id = acct.id
        rp.save(update_fields=["stripe_account_id"])

    # Always create a fresh onboarding link
    link = stripe.AccountLink.create(
        account=rp.stripe_account_id,
        type="account_onboarding",
        refresh_url=_abs(request, "core:connect_onboard_start"),
        return_url=_abs(request, "core:connect_onboard_return"),
    )
    return redirect(link.url)



@login_required
@require_http_methods(["GET"])
def connect_onboard_return(request: HttpRequest) -> HttpResponse:
    """
    Return URL after Stripe Connect onboarding.
    - Locate the restaurant (session first, then ?account= fallback)
    - Pull latest Stripe account details
    - Update RestaurantProfile with name/email/phone/address and stripe_account_id
    - Cache charges/payouts flags for quick checks
    """
    # --- locate restaurant ---
    rp = _get_current_restaurant(request)

    # If the session got lost, Stripe sometimes returns ?account=acct_...
    acct_param = (request.GET.get("account") or "").strip()
    if not rp and acct_param:
        rp = RestaurantProfile.objects.filter(stripe_account_id=acct_param).first()

    if not rp:
        return HttpResponseBadRequest("Missing restaurant context.")
    
    if not rp.stripe_account_id and acct_param:
        # If we arrived with ?account=acct_... but the model wasn’t set yet, set it now
        rp.stripe_account_id = acct_param
        rp.save(update_fields=["stripe_account_id"])

    if not rp.stripe_account_id:
        return HttpResponseBadRequest("Restaurant has no Stripe account id.")

    # --- fetch latest Stripe account ---
    try:
        acct = stripe.Account.retrieve(rp.stripe_account_id)
    except Exception as e:
        return HttpResponseBadRequest(f"Could not retrieve Stripe account: {e}")

    # --- pull fields safely from Stripe ---
    # Prefer company details for business entities; fall back to business_profile where useful.
    company      = acct.get("company") or {}
    comp_addr    = company.get("address") or {}
    biz_profile  = acct.get("business_profile") or {}

    # Names
    legal_name   = (company.get("name") or "").strip()
    dba_name     = (biz_profile.get("name") or "").strip()

    # Contact
    support_email = (biz_profile.get("support_email") or "").strip()
    support_phone = (biz_profile.get("support_phone") or "").strip()

    # Address
    line1   = (comp_addr.get("line1") or "").strip()
    line2   = (comp_addr.get("line2") or "").strip()
    city    = (comp_addr.get("city") or "").strip()
    state   = (comp_addr.get("state") or "").strip()
    postal  = (comp_addr.get("postal_code") or "").strip()

    # Cached flags
    cached = {
        "details_submitted": bool(acct.get("details_submitted")),
        "charges_enabled":   bool(acct.get("charges_enabled")),
        "payouts_enabled":   bool(acct.get("payouts_enabled")),
        "business_profile": {
            "name":          dba_name or None,
            "support_email": support_email or None,
            "support_phone": support_phone or None,
        },
        "company": {
            "name":    legal_name or None,
            "address": {
                "line1": line1 or None,
                "line2": line2 or None,
                "city":  city or None,
                "state": state or None,
                "postal": postal or None,
            },
        },
    }

    # --- apply to RestaurantProfile (only what changed) ---
    changed: set[str] = set()

    # Always persist the Stripe account id we used
    if acct.get("id") and acct["id"] != rp.stripe_account_id:
        rp.stripe_account_id = acct["id"]
        changed.add("stripe_account_id")

    def _set(field: str, value: str):
        if value is None:
            value = ""
        if getattr(rp, field, "") != value:
            setattr(rp, field, value)
            changed.add(field)

    # Map into your model fields
    if legal_name:
        _set("legal_name", legal_name)
    if dba_name:
        _set("dba_name", dba_name)

    # Only overwrite email/phone if Stripe has them and your field is empty or different
    if support_email:
        _set("email", support_email)
    if support_phone:
        _set("phone", support_phone)

    # Address lines
    if line1:  _set("addr_line1", line1)
    if line2:  _set("addr_line2", line2)
    if city:   _set("city", city)
    if state:  _set("state", state)
    if postal: _set("postal", postal)

    # Cache snapshot + optionally mark active
    if getattr(rp, "stripe_cached", None) != cached:
        rp.stripe_cached = cached
        changed.add("stripe_cached")

    if not rp.is_active:
        rp.is_active = True
        changed.add("is_active")

    if changed:
        rp.save(update_fields=list(changed))

    return redirect(reverse("core:owner_dashboard"))


@login_required
@require_http_methods(["GET"])
def connect_dashboard_login(request: HttpRequest):
    """
    Optional: let owners jump into their Stripe Dashboard for the connected account.
    """
    rp = _get_current_restaurant(request)
    if not rp or not rp.stripe_account_id:
        return HttpResponseBadRequest("Missing restaurant or Stripe account id.")
    login_link = stripe.Account.create_login_link(rp.stripe_account_id)
    return redirect(login_link.url)


# --- Helper your app can call anywhere to read the latest from Stripe ---

def get_restaurant_stripe_profile(rp: RestaurantProfile, live_fetch: bool = False) -> dict:
    """
    Returns a dict with business_profile name and company address from Stripe.
    If live_fetch=True, refreshes from Stripe; else uses cached first.
    """
    if not rp.stripe_account_id:
        return {}

    if live_fetch or not rp.stripe_cached:
        acct = stripe.Account.retrieve(rp.stripe_account_id)
        rp.stripe_cached = {
            "details_submitted": bool(acct.get("details_submitted")),
            "charges_enabled": bool(acct.get("charges_enabled")),
            "payouts_enabled": bool(acct.get("payouts_enabled")),
            "business_profile": {
                "name": (acct.get("business_profile") or {}).get("name"),
            },
            "company": {
                "name": (acct.get("company") or {}).get("name"),
                "address": (acct.get("company") or {}).get("address") or {},
            },
        }
        rp.save(update_fields=["stripe_cached"])
    return rp.stripe_cached or {}

# core/views_webhooks.py
import json
from typing import Optional

from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from decouple import config
import stripe

from core.models import RestaurantProfile, OwnerProfile

stripe.api_key = config("STRIPE_SK")
WEBHOOK_SECRET = config("STRIPE_WH_OWNER", default="")


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def _pick_names(acct: dict) -> tuple[str, str]:
    bp = acct.get("business_profile") or {}
    company = acct.get("company") or {}
    individual = acct.get("individual") or {}

    legal = _norm(company.get("name"))
    if not legal and (individual.get("first_name") or individual.get("last_name")):
        legal = _norm(" ".join(x for x in [individual.get("first_name"), individual.get("last_name")] if x))

    dba = _norm(bp.get("name")) or legal
    return legal, dba


def _pick_address(acct: dict) -> dict:
    company    = acct.get("company") or {}
    individual = acct.get("individual") or {}
    addr       = company.get("address") or individual.get("address") or {}
    return {
        "addr_line1": _norm(addr.get("line1")),
        "addr_line2": _norm(addr.get("line2")),
        "city":       _norm(addr.get("city")),
        "state":      _norm(addr.get("state")),
        "postal":     _norm(addr.get("postal_code")),
    }


def _link_owner(rp: RestaurantProfile, owner: OwnerProfile):
    try:
        rp.owners.add(owner)
        return
    except Exception:
        from core.models import Ownership
        Ownership.objects.get_or_create(restaurant=rp, owner=owner)


@csrf_exempt
def stripe_owner_webhook(request: HttpRequest):
    # Verify signature (HIGHLY recommended outside dev)
    payload = request.body
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET) if WEBHOOK_SECRET else json.loads(payload.decode() or "{}")
    except Exception as e:
        return HttpResponseBadRequest(f"Invalid payload/signature: {e}")

    evt_type = event.get("type")
    acct_id  = event.get("account")  # present for Connect events
    obj      = (event.get("data") or {}).get("object") or {}

    try:
        if evt_type == "account.created":
            _handle_account_created(obj)
        elif evt_type == "account.updated" and acct_id:
            _handle_account_updated(acct_id, obj)
        elif evt_type == "account.application.deauthorized" and acct_id:
            _handle_account_deauthorized(acct_id)
    except Exception as e:
        print(f"[stripe_owner_webhook] ERROR {evt_type} {acct_id}: {e}")

    return HttpResponse(status=200)


@transaction.atomic
def _handle_account_created(acct_obj: dict):
    acct_id = acct_obj.get("id")
    if not acct_id or not str(acct_id).startswith("acct_"):
        return

    # Try to find existing by stripe_account_id
    rp = RestaurantProfile.objects.filter(stripe_account_id=acct_id).first()
    meta = acct_obj.get("metadata") or {}

    # If we passed a placeholder restaurant_id or owner via metadata, use it
    if not rp:
        rid = _norm(meta.get("restaurant_id"))
        if rid:
            rp = RestaurantProfile.objects.filter(id=rid).first()

    legal, dba = _pick_names(acct_obj)
    addr = _pick_address(acct_obj)
    email = _norm(acct_obj.get("email"))
    active = bool(acct_obj.get("charges_enabled")) and bool(acct_obj.get("payouts_enabled"))

    if rp:
        rp.stripe_account_id = acct_id
        if legal: rp.legal_name = legal
        if dba:   rp.dba_name   = dba
        if email: rp.email      = email
        for k, v in addr.items():
            if v: setattr(rp, k, v)
        rp.is_active = active
        rp.save()
    else:
        rp = RestaurantProfile.objects.create(
            legal_name = legal or "Unknown Restaurant",
            dba_name   = dba or legal or "Unknown Restaurant",
            email      = email or "",
            phone      = "",
            addr_line1 = addr["addr_line1"],
            addr_line2 = addr["addr_line2"],
            city       = addr["city"],
            state      = addr["state"],
            postal     = addr["postal"],
            omnivore_location_id = "",
            is_active  = active,
            stripe_account_id = acct_id,
        )

    # Optionally link owner from metadata
    owner_user_id = _norm(meta.get("owner_user_id"))
    if owner_user_id:
        try:
            owner = OwnerProfile.objects.select_related("user").get(user_id=int(owner_user_id))
            _link_owner(rp, owner)
        except OwnerProfile.DoesNotExist:
            pass


@transaction.atomic
def _handle_account_updated(acct_id: str, acct_obj: dict):
    rp = RestaurantProfile.objects.filter(stripe_account_id=acct_id).first()
    if not rp:
        return _handle_account_created(acct_obj)

    legal, dba = _pick_names(acct_obj)
    addr = _pick_address(acct_obj)
    email = _norm(acct_obj.get("email"))
    active = bool(acct_obj.get("charges_enabled")) and bool(acct_obj.get("payouts_enabled"))

    if legal: rp.legal_name = legal
    if dba:   rp.dba_name   = dba
    if email: rp.email      = email
    for k, v in addr.items():
        if v: setattr(rp, k, v)
    rp.is_active = active
    rp.save()


@transaction.atomic
def _handle_account_deauthorized(acct_id: str):
    rp = RestaurantProfile.objects.filter(stripe_account_id=acct_id).first()
    if not rp:
        return
    rp.is_active = False
    rp.save(update_fields=["is_active"])
