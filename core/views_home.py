# core/views_home.py
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, redirect, resolve_url
from django.utils import timezone
from django.utils.html import mark_safe
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.views.decorators.http import require_GET, require_POST

# Third-party config
from decouple import config
import stripe

# Set Stripe secret (keeps prior behavior)
stripe.api_key = config("STRIPE_SK")

# Local imports
from .models import (
    CustomerProfile,
    Member,
    TicketLink,
    RestaurantProfile,
    Review,  # <-- make sure Review model exists as discussed
)
from .omnivore import (
    get_ticket,
    get_ticket_items,
    create_payment_with_tender_type,
)
from core.views_processing import (
    charge_customer_off_session,
    refund_payment_intent,
    PaymentError,
    build_idem_key,
)


# --------------------
# Small helpers (unchanged semantics)
# --------------------
def _member_for_user(user) -> Optional[Member]:
    if not getattr(user, "is_authenticated", False):
        return None
    cp = getattr(user, "customer_profile", None)
    if not cp:
        return None
    return Member.objects.filter(customer=cp).first()


def _user_has_customer(user) -> bool:
    return bool(getattr(user, "customer_profile", None))


def _due_member_for_user(user) -> Optional[Member]:
    """
    Most recently-created Member for ANY CustomerProfile owned by this user.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        Member.objects
        .filter(customer__user=user)
        .order_by("-id")
        .first()
    )


def _money_cents_from_ticket(t: dict) -> tuple[int, int, int, int]:
    """
    Returns (subtotal, tax, total, due) in cents with sensible fallbacks.
    """
    items = t.get("items", []) if t else []
    totals = (t or {}).get("totals") or {}

    subtotal_calc = 0
    for i in items:
        try:
            qty = int(i.get("quantity", 1) or 1)
            cents = int(i.get("price", 0) or 0)
        except Exception:
            qty, cents = 1, 0
        subtotal_calc += qty * cents

    subtotal = int(totals.get("sub_total", subtotal_calc) or subtotal_calc)
    tax      = int(totals.get("tax", 0) or 0)
    total    = int(totals.get("total", subtotal + tax) or (subtotal + tax))
    due      = int(totals.get("due", total) or total)
    return subtotal, tax, total, due


# --------------------
# Home
# --------------------
@ensure_csrf_cookie
def customer_home(request: HttpRequest) -> HttpResponse:
    """
    Render the profile/home page with:
      - has_customer, has_live_order, member_number
      - Stripe card brand/last4
      - maps_api_key
      - restaurants_json (for the map; can be empty and front-end will use demo)
    """
    user = request.user
    has_customer = False
    has_live_order = False
    member_number = ""
    card_brand = ""
    card_last4 = ""

    if user.is_authenticated:
        # Do they have any customer profile?
        cp = CustomerProfile.objects.filter(user=user).order_by("-id").first()
        has_customer = cp is not None

        # Prefer newest open link
        open_link = (
            TicketLink.objects
            .filter(member__customer__user=user, status="open")
            .select_related("member")
            .order_by("-opened_at")
            .first()
        )
        if open_link:
            has_live_order = True
            member_number = (open_link.member.number or "").strip()
        else:
            # Fallback: latest member for this user
            m = (
                Member.objects
                .filter(customer__user=user)
                .order_by("-id")
                .first()
            )
            if m:
                member_number = (m.number or "").strip()

        # Stripe card details (brand/last4)
        if cp and getattr(cp, "stripe_customer_id", None):
            pm_id = (getattr(cp, "default_payment_method", "") or "").strip()
            try:
                if not pm_id:
                    cust = stripe.Customer.retrieve(cp.stripe_customer_id)
                    pm_id = (cust.get("invoice_settings", {}) or {}).get("default_payment_method") or ""
                if not pm_id:
                    pms = stripe.PaymentMethod.list(
                        customer=cp.stripe_customer_id,
                        type="card",
                        limit=1,
                    )
                    if pms and pms.data:
                        pm_id = pms.data[0].id
                if pm_id:
                    pm = stripe.PaymentMethod.retrieve(pm_id)
                    card = (pm or {}).get("card") or {}
                    if card:
                        brand = (card.get("brand") or "").strip()
                        card_brand = brand[:1].upper() + brand[1:] if brand else ""
                        card_last4 = (card.get("last4") or "").strip()
            except Exception:
                # Page still renders fine if Stripe fails
                pass

    # Google Maps API key
    maps_api_key = config("GOOGLE_MAPS_API", default="")

    # Restaurants payload for the map (optional)
    def fmt_addr(r: RestaurantProfile) -> str:
        parts = [r.addr_line1, r.city, r.state]
        return ", ".join([p for p in parts if p])

    restaurants = []
    for r in RestaurantProfile.objects.filter(is_active=True).order_by("id"):
        restaurants.append({
            "id": r.id,
            "name": r.display_name(),
            "address": fmt_addr(r) or "",
            "city": (r.city or ""),
            # If you add lat/lng fields later, include them to skip geocoding on the client
            # "lat": r.lat, "lng": r.lng,
            "tx": (r.stripe_cached or {}).get("tx_14d"),
        })

    ctx = {
        "has_customer": has_customer,
        "has_live_order": has_live_order,
        "member_number": member_number,
        "card_brand": card_brand,
        "card_last4": card_last4,
        "maps_api_key": maps_api_key,
        "restaurants_json": mark_safe(json.dumps(restaurants)),
    }
    return render(request, "core/profile.html", ctx)


# --------------------
# Signout (unchanged)
# --------------------
def signout(request: HttpRequest) -> HttpResponse:
    logout(request)
    nxt = request.GET.get("next")
    if nxt and nxt.startswith("/"):
        return redirect(nxt)
    return redirect(resolve_url("core:profile"))


# --------------------
# Live ticket receipt (used by /api/member/<member>/receipt)
# --------------------
@require_GET
def api_ticket_receipt(request: HttpRequest, member_number: str) -> JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Auth required."}, status=401)

    m = Member.objects.filter(number=str(member_number), customer__user=request.user).first()
    if not m:
        return JsonResponse({"ok": False, "error": "Not authorized for this member."}, status=403)

    tl = (TicketLink.objects
          .filter(member=m, status="open")
          .select_related("restaurant")
          .order_by("-opened_at")
          .first())
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    rp: RestaurantProfile = tl.restaurant
    loc_id = (rp.omnivore_location_id or "").strip()
    if not loc_id:
        return JsonResponse({"ok": False, "error": "Restaurant not wired to POS."}, status=500)

    t = get_ticket(loc_id, tl.ticket_id) or {}
    items = get_ticket_items(loc_id, tl.ticket_id) or []

    rows, subtotal_calc = [], 0
    for i in items:
        qty = int(i.get("quantity", 1) or 1)
        cents = int(i.get("price", 0) or 0)
        subtotal_calc += qty * cents
        rows.append({"name": i.get("name"), "qty": qty, "cents": cents})

    totals = (t or {}).get("totals") or {}
    to_int = lambda v: int(v) if v not in (None, "") else 0

    subtotal   = to_int(totals.get("subtotal") or totals.get("sub_total") or subtotal_calc)
    tax        = to_int(totals.get("tax"))
    base_total = subtotal + tax

    # Always treat our base (subtotal+tax) as "due" for live-view consistency
    TicketLink.objects.filter(pk=tl.pk).update(last_total_cents=base_total)

    return JsonResponse({
        "ok": True,
        "ticket_id": tl.ticket_id,
        "server": tl.server_name or "",
        "items": rows,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": base_total,   # UI adds tip on top of this
        "due_cents": base_total,     # keep UI aligned; or drop this field if unused

        # 👇 New: server chat bubble content
        "server_message": "Thank you for dining with us!",
        "server_message_by": "Amanda",
    })



# --------------------
# Customer close tab (used by /api/member/<member>/close)
# Includes review context in success JSON
# --------------------
@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_close_tab(request: HttpRequest, member: str) -> JsonResponse:
    """
    Customer close (Stripe Connect version)
    Body: {"tip_cents": <int>, "reference": "customer-close"}

    Changes:
      - Compute base_due = subtotal + tax (ignore POS 'due'/'total' quirks)
      - Charge Stripe for base_due + tip
      - POS post with fallback: (amount=base, tip=tip) -> else (amount=base+tip, tip=0)
      - Snapshot consistent numbers into TicketLink
    """
    # ---------- helpers ----------
    def _get_embedded_list(obj, key):
        if not obj:
            return []
        emb = obj.get("_embedded") or {}
        if isinstance(emb, dict) and isinstance(emb.get(key), list):
            return emb.get(key) or []
        if isinstance(obj.get(key), list):
            return obj.get(key) or []
        return []

    def _normalize_modifiers(line_item: dict) -> list:
        mods = []
        for bucket in ("modifiers", "options", "applied_modifiers"):
            for m in _get_embedded_list(line_item, bucket):
                mods.append({
                    "id":          str(m.get("id") or m.get("modifier_id") or m.get("pos_id") or ""),
                    "name":        m.get("name") or "",
                    "qty":         int(m.get("quantity") or 1),
                    "price_cents": int(m.get("price") or m.get("price_per_unit") or 0),
                    "raw":         m,
                })
        return mods

    def _normalize_line_items(ticket_json: dict) -> list:
        items = []
        line_items = _get_embedded_list(ticket_json, "items") or _get_embedded_list(ticket_json, "line_items")
        for li in line_items:
            mi_val = li.get("menu_item")
            if isinstance(mi_val, dict):
                menu_item_id = str(mi_val.get("id") or mi_val.get("pos_id") or "")
                menu_item_name = mi_val.get("name") or li.get("name") or ""
            else:
                menu_item_id = str(mi_val or li.get("menu_item_id") or li.get("pos_id") or "")
                menu_item_name = li.get("name") or ""

            item_obj = {
                "menu_item_id": menu_item_id,
                "name":         menu_item_name,
                "qty":          int(li.get("quantity") or 1),
                "seat":         li.get("seat"),
                "price_level":  str(li.get("price_level") or li.get("price_level_id")) if li.get("price_level") or li.get("price_level_id") else None,
                "price_cents":  int(li.get("price") or li.get("price_per_unit") or li.get("unit_price") or 0),
                "total_cents":  int(li.get("total") or li.get("extended_price") or 0),
                "voided":       bool(li.get("void") or li.get("voided") or False),
                "mods":         _normalize_modifiers(li),
                "raw":          li,
            }
            items.append(item_obj)
        return items

    def _totals_from_ticket(ticket_json: dict) -> dict:
        totals = (ticket_json or {}).get("totals") or {}
        to_int = lambda v: int(v) if v not in (None, "") else 0
        sub_val = totals.get("subtotal", totals.get("sub_total", 0))
        return {
            "subtotal_cents":        to_int(sub_val),
            "tax_cents":             to_int(totals.get("tax")),
            "discounts_cents":       to_int(totals.get("discounts") or totals.get("discount") or 0),
            "total_cents":           to_int(totals.get("total")),   # POS may include svc/fees here
            "due_cents":             to_int(totals.get("due")),     # often unreliable
            "service_charge_cents":  to_int(totals.get("service_charge") or totals.get("svc_charge") or 0),
        }

    def _fill_merchant_snapshot(link: TicketLink, rp: RestaurantProfile):
        if not link.merchant_name:
            link.merchant_name = rp.display_name() or (getattr(rp, "legal_name", "") or "")
        if not link.merchant_addr1:
            link.merchant_addr1 = getattr(rp, "address1", "") or ""
        if not link.merchant_addr2:
            link.merchant_addr2 = getattr(rp, "address2", "") or ""
        if not link.merchant_city:
            link.merchant_city = getattr(rp, "city", "") or ""
        if not link.merchant_state:
            link.merchant_state = getattr(rp, "state", "") or ""
        if not link.merchant_zip:
            link.merchant_zip = getattr(rp, "zipcode", "") or ""
        if not link.merchant_phone:
            link.merchant_phone = getattr(rp, "phone", "") or ""

    def _compute_base_due(ticket_json: dict) -> int:
        """Return subtotal+tax (in cents). Fall back to summing embedded items."""
        to_int = lambda v: int(v) if v not in (None, "") else 0
        totals = (ticket_json or {}).get("totals") or {}
        sub = to_int(totals.get("sub_total", totals.get("subtotal", 0)))
        tax = to_int(totals.get("tax"))
        if sub <= 0:
            # Sum embedded items if subtotal not present
            sub_calc = 0
            for li in _get_embedded_list(ticket_json, "items") or _get_embedded_list(ticket_json, "line_items"):
                qty = int(li.get("quantity") or 1)
                # prefer unit price * qty; else extended "total"
                unit = int(li.get("price") or li.get("price_per_unit") or li.get("unit_price") or 0)
                if unit:
                    sub_calc += qty * unit
                else:
                    sub_calc += int(li.get("total") or 0)
            sub = sub_calc
        return max(sub + tax, 0)

    # ---------- auth ----------
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "auth_required"}, status=401)

    # ---------- member belongs to user ----------
    m = (
        Member.objects
        .select_related("customer")
        .filter(number=str(member), customer__user=request.user)
        .first()
    )
    if not m:
        return JsonResponse({"ok": False, "error": "not_authorized_for_member"}, status=403)

    # ---------- open ticket ----------
    tl = (
        TicketLink.objects
        .select_related("restaurant", "member", "member__customer")
        .filter(member=m, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "no_active_ticket"}, status=404)

    rp: RestaurantProfile = tl.restaurant
    loc_id = (rp.omnivore_location_id or "").strip()
    if not loc_id:
        return JsonResponse({"ok": False, "error": "restaurant_missing_location_id"}, status=500)

    # ---------- body ----------
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}
    reference = (data.get("reference") or "customer-close").strip()

    try:
        tip_cents = max(0, int(data.get("tip_cents") or 0))
    except Exception:
        tip_cents = 0

    # ---------- compute base due from POS (subtotal + tax) ----------
    ticket_json = {}
    try:
        ticket_json = get_ticket(loc_id, tl.ticket_id) or {}
    except Exception:
        ticket_json = {}

    base_due = _compute_base_due(ticket_json)

    # LAST RESORT: stale snapshot if POS unreachable and we have something sane
    if base_due <= 0:
        base_due = max(
            [int(x.last_total_cents or 0) for x in TicketLink.objects.filter(ticket_id=tl.ticket_id, status="open")] or [0]
        )
    if base_due <= 0:
        return JsonResponse({"ok": False, "error": "nothing_due"}, status=400)

    gross_cents = base_due + tip_cents  # amount we charge on Stripe

    # ---------- customer + PM ----------
    cp: CustomerProfile | None = getattr(m, "customer", None)
    if not cp or not cp.stripe_customer_id or not cp.default_payment_method:
        return JsonResponse({"ok": False, "error": "customer_missing_payment_method"}, status=400)

    # ---------- Stripe charge ----------
    stripe_meta = {
        "ticket_id": tl.ticket_id,
        "restaurant_id": str(rp.id),
        "member_number": m.number,
        "customer_profile_id": str(cp.id),
        "source": "customer_close",
    }
    description = f"Dine N Dash — Ticket {tl.ticket_id} ({rp.display_name()})"
    idem_key = build_idem_key("cust_close", {
        "amount": gross_cents,
        "customer": cp.stripe_customer_id,
        "pm": cp.default_payment_method,
        "restaurant": rp.stripe_account_id or "",
        "ticket": tl.ticket_id,
        "tip": tip_cents,
    })

    try:
        intent = charge_customer_off_session(
            customer_id=cp.stripe_customer_id,
            payment_method_id=cp.default_payment_method,
            amount_cents=gross_cents,
            currency="usd",
            description=description,
            idempotency_key=idem_key,
            metadata=stripe_meta,
            destination_account_id=(rp.stripe_account_id or None),
            on_behalf_of=(rp.stripe_account_id or None),
        )
    except PaymentError as e:
        return JsonResponse({
            "ok": False,
            "error": "stripe_charge_failed",
            "detail": str(e),
            "code": e.code,
            "decline_code": e.decline_code,
            "payment_intent": e.payment_intent_id,
        }, status=402)

    # ---------- POS post with fallback ----------
    def _post_to_pos(amount_cents: int, tip_cents_val: int):
        return create_payment_with_tender_type(
            location_id=loc_id,
            ticket_id=tl.ticket_id,
            amount_cents=amount_cents,
            tender_type_id=None,   # pass a specific tender_type_id if POS requires it
            reference=reference,
            tip_cents=tip_cents_val,
        )

    try:
        try:
            # scheme A: amount=base, tip=tip
            _post_to_pos(base_due, tip_cents)
        except Exception as first_err:
            # scheme B: amount includes tip, tip=0
            _post_to_pos(base_due + tip_cents, 0)
    except Exception as pos_err:
        # refund Stripe if POS failed
        try:
            if getattr(intent, "id", None):
                refund_payment_intent(intent.id, reason="requested_by_customer")
        except PaymentError as refund_err:
            return JsonResponse({
                "ok": False,
                "error": "pos_post_failed_and_refund_failed",
                "pos_detail": str(pos_err),
                "refund_detail": str(refund_err),
                "payment_intent": getattr(intent, "id", None),
            }, status=502)

        return JsonResponse({
            "ok": False,
            "error": "omnivore_payment_failed_refunded",
            "pos_detail": str(pos_err),
            "payment_intent": getattr(intent, "id", None),
        }, status=502)

    # ---------- snapshot + close links ----------
    now = timezone.now()
    open_links = list(TicketLink.objects.filter(ticket_id=tl.ticket_id, status="open"))

    try:
        normalized_items = _normalize_line_items(ticket_json) if ticket_json else []
    except Exception:
        normalized_items = []

    # Prefer our computed base_due for total_cents to keep it consistent with the charge
    totals_from_pos = {}
    try:
        totals_from_pos = _totals_from_ticket(ticket_json) if ticket_json else {}
    except Exception:
        totals_from_pos = {}

    update_fields = [
        "status", "closed_at",
        "total_cents", "tip_cents", "paid_cents",
        "discounts_cents", "subtotal_cents", "tax_cents",
        "items_json", "raw_ticket_json", "pos_ref",
        "merchant_name", "merchant_addr1", "merchant_addr2",
        "merchant_city", "merchant_state", "merchant_zip", "merchant_phone",
    ]

    for link in open_links:
        _fill_merchant_snapshot(link, rp)

        link.status = "closed"
        link.closed_at = now

        link.subtotal_cents  = int(totals_from_pos.get("subtotal_cents") or 0)
        link.tax_cents       = int(totals_from_pos.get("tax_cents") or 0)
        link.discounts_cents = int(totals_from_pos.get("discounts_cents") or 0)

        link.total_cents     = int(base_due)             # <= our authoritative base amount (subtotal+tax)
        link.tip_cents       = int(tip_cents or 0)
        link.paid_cents      = int(gross_cents or 0)

        link.items_json      = normalized_items or link.items_json or []
        link.raw_ticket_json = ticket_json or link.raw_ticket_json or {}

        link.pos_ref = reference

        link.save(update_fields=update_fields)

    return JsonResponse({
        "ok": True,
        "closed": len(open_links),
        "paid_cents": gross_cents,
        "tip_cents": tip_cents,
        "base_due_cents": base_due,             # for visibility while testing
        "payment_intent": getattr(intent, "id", None),
        "destination": rp.stripe_account_id or None,
        "review": {
            "restaurant_id": rp.id,
            "restaurant_name": rp.display_name(),
            "ticket_link_id": tl.id,
        },
    })



# --------------------
# Submit a review
# --------------------
@require_POST
@csrf_protect
@login_required
def api_submit_review(request: HttpRequest) -> JsonResponse:
    """
    Body JSON:
      { "restaurant_id": int, "ticket_link_id": int|null, "stars": 1..5, "comment": "..." }

    Guards:
      - user must be authenticated
      - if ticket_link_id provided, it must belong to THIS user (via Member->CustomerProfile->user)
      - one review per ticket_link
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}

    rid  = data.get("restaurant_id")
    tlid = data.get("ticket_link_id")

    try:
        stars = int(data.get("stars") or 0)
    except Exception:
        stars = 0

    comment = (data.get("comment") or "").strip()

    if not rid or not (1 <= stars <= 5):
        return JsonResponse({"ok": False, "error": "invalid_input"}, status=400)

    rp = RestaurantProfile.objects.filter(id=rid, is_active=True).first()
    if not rp:
        return JsonResponse({"ok": False, "error": "restaurant_not_found"}, status=404)

    tl = None
    m = None

    if tlid:
        # Load the ticket and verify it belongs to the signed-in user
        tl = (
            TicketLink.objects
            .select_related("member", "member__customer")
            .filter(id=tlid)
            .first()
        )
        if not tl:
            return JsonResponse({"ok": False, "error": "ticket_not_found"}, status=404)

        # Authz: the ticket's member must belong to this user
        if not tl.member or not tl.member.customer or tl.member.customer.user_id != request.user.id:
            return JsonResponse({"ok": False, "error": "not_authorized_for_ticket"}, status=403)

        m = tl.member

        # Enforce one review per ticket
        if Review.objects.filter(ticket_link=tl).exists():
            return JsonResponse({"ok": False, "error": "already_reviewed"}, status=409)
    else:
        # No ticket given — optionally attach the latest member for this user (or leave None)
        m = (
            Member.objects
            .filter(customer__user=request.user)
            .order_by("-id")
            .first()
        )

    # Create the review (no `user` field in your model)
    rev = Review.objects.create(
        restaurant=rp,
        ticket_link=tl,
        member=m,
        stars=stars,
        comment=comment,
    )

    return JsonResponse({"ok": True, "id": rev.id})

# -------- Real customer transactions + receipt + review --------
from django.views.decorators.http import require_http_methods

@require_GET
def api_me_transactions(request: HttpRequest) -> JsonResponse:
    """
    Return the user's most recent closed TicketLinks.
    Each row includes enough to render the table and hook actions.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "auth_required"}, status=401)

    q = (
        TicketLink.objects
        .select_related("restaurant", "member", "member__customer")
        .filter(member__customer__user=request.user, status="closed")
        .order_by("-closed_at", "-id")
    )

    results = []
    for tl in q[:200]:
        rp = tl.restaurant
        results.append({
            "ticket_link_id": tl.id,
            "ticket_id": tl.ticket_id,
            "closed_at": (tl.closed_at.isoformat() if tl.closed_at else None),
            "restaurant_id": rp.id,
            "restaurant_name": rp.display_name(),
            "restaurant_city": getattr(rp, "city", "") or "",
            "subtotal_cents": int(tl.subtotal_cents or 0),
            "tax_cents": int(tl.tax_cents or 0),
            "tip_cents": int(tl.tip_cents or 0),
            "total_cents": int(tl.total_cents or 0),  # base (subtotal+tax)
            "paid_cents": int(tl.paid_cents or (int(tl.total_cents or 0) + int(tl.tip_cents or 0))),
        })
    return JsonResponse({"ok": True, "results": results})


@require_GET
def api_ticket_link_receipt(request: HttpRequest, tl_id: int) -> JsonResponse:
    """
    Itemized receipt for a CLOSED ticket link owned by the user.
    Uses the normalized items_json captured at close-out.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "auth_required"}, status=401)

    tl = (
        TicketLink.objects
        .select_related("restaurant", "member", "member__customer")
        .filter(id=int(tl_id), member__customer__user=request.user, status="closed")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)

    # Build items from normalized snapshot
    items_src = tl.items_json or []
    items = []
    sub_calc = 0
    for it in items_src:
        qty = int(it.get("qty") or it.get("quantity") or 1)
        unit = int(it.get("price_cents") or it.get("unit_cents") or 0)
        line = int(it.get("total_cents") or (qty * unit))
        sub_calc += line
        items.append({
            "name": it.get("name") or "",
            "qty": qty,
            "unit_cents": unit,
            "line_total_cents": line,
        })

    subtotal = int(tl.subtotal_cents or (sub_calc))
    tax      = int(tl.tax_cents or 0)
    tip      = int(tl.tip_cents or 0)
    total    = int(tl.total_cents or (subtotal + tax))
    paid     = int(tl.paid_cents or (total + tip))

    return JsonResponse({
        "ok": True,
        "ticket_number": str(tl.ticket_id),
        "server": tl.server_name or "",
        "restaurant": tl.restaurant.display_name(),
        "items": items,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "tip_cents": tip,
        "total_cents": total,   # base (subtotal + tax)
        "paid_cents": paid,     # what customer actually paid (includes tip)
        "closed_at": tl.closed_at.isoformat() if tl.closed_at else None,
    })


@require_POST
@csrf_protect
@login_required
def api_review_submit(request: HttpRequest) -> JsonResponse:
    """
    UPSERT a review.

    Body JSON:
      { "restaurant_id": int, "ticket_link_id": int|null, "stars": 1..5, "comment": "..." }

    Rules / behavior:
      - auth required
      - if ticket_link_id provided, it must belong to THIS user
      - if a review already exists for that ticket_link, UPDATE it (no 409)
      - Review model has NO user field; we attach restaurant, ticket_link, member
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}

    rid  = int(data.get("restaurant_id") or 0)
    tlid = int(data.get("ticket_link_id") or 0)
    try:
        stars = int(data.get("stars") or 0)
    except Exception:
        stars = 0
    comment = (data.get("comment") or "").strip()

    if not rid or not (1 <= stars <= 5):
        return JsonResponse({"ok": False, "error": "invalid_input"}, status=400)

    rp = RestaurantProfile.objects.filter(id=rid, is_active=True).first()
    if not rp:
        return JsonResponse({"ok": False, "error": "restaurant_not_found"}, status=404)

    tl = None
    m  = None

    if tlid:
        # Verify the ticket belongs to this user
        tl = (
            TicketLink.objects
            .select_related("member", "member__customer")
            .filter(id=tlid)
            .first()
        )
        if not tl:
            return JsonResponse({"ok": False, "error": "ticket_not_found"}, status=404)

        if not tl.member or not tl.member.customer or tl.member.customer.user_id != request.user.id:
            return JsonResponse({"ok": False, "error": "not_authorized_for_ticket"}, status=403)

        m = tl.member
        # --- UPSERT: update if exists, else create ---
        rev = Review.objects.filter(ticket_link=tl).first()
        if rev:
            rev.stars   = stars
            rev.comment = comment
            # keep associations in sync in case anything changed
            if not rev.restaurant_id: rev.restaurant = rp
            if not rev.member_id:     rev.member     = m
            rev.save(update_fields=["stars", "comment", "restaurant", "member", "updated_at"])
            return JsonResponse({"ok": True, "id": rev.id, "updated": True})
        else:
            rev = Review.objects.create(
                restaurant=rp,
                ticket_link=tl,
                member=m,
                stars=stars,
                comment=comment,
            )
            return JsonResponse({"ok": True, "id": rev.id, "created": True})
    else:
        # No ticket provided — attach the latest member for this user (optional)
        m = (
            Member.objects
            .filter(customer__user=request.user)
            .order_by("-id")
            .first()
        )
        rev = Review.objects.create(
            restaurant=rp,
            ticket_link=None,
            member=m,
            stars=stars,
            comment=comment,
        )
        return JsonResponse({"ok": True, "id": rev.id, "created": True})
    return JsonResponse({"ok": True, "id": rev.id})
# --- Reviews API -------------------------------------------------------------
from django.views.decorators.http import require_http_methods
from django.db.models import Q

def _owned_ticketlink(request, tl_id: int):
    """Return TicketLink if it belongs to the signed-in user, else None."""
    if not request.user.is_authenticated:
        return None
    return (
        TicketLink.objects
        .select_related("member", "member__customer", "restaurant")
        .filter(id=int(tl_id), member__customer__user=request.user)
        .first()
    )

@require_GET
@login_required
def api_review_for_ticket(request: HttpRequest, ticket_link_id: int) -> JsonResponse:
    """
    Returns the current user's review for this ticket link (or null).
    """
    tl = (
        TicketLink.objects
        .select_related("member__customer", "restaurant")
        .filter(pk=ticket_link_id, member__customer__user=request.user)
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "not_authorized_for_ticket"}, status=403)

    rev = Review.objects.filter(ticket_link=tl).first()
    return JsonResponse({"ok": True, "review": _serialize_review(rev) if rev else None})


@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_review_save(request: HttpRequest) -> JsonResponse:
    """
    Create or update a review for a ticket (exactly one per ticket_link).
    Body JSON:
      - ticket_link_id (required)
      - id (optional; when editing a previously created review)
      - stars (1..5)
      - comment (string)
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "auth_required"}, status=401)

    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}

    try:
        tl_id = int(data.get("ticket_link_id") or 0)
    except Exception:
        tl_id = 0

    if not tl_id:
        return JsonResponse({"ok": False, "error": "ticket_link_id_required"}, status=400)

    tl = _owned_ticketlink(request, tl_id)
    if not tl:
        return JsonResponse({"ok": False, "error": "not_authorized_or_missing_ticket"}, status=404)

    try:
        stars = int(data.get("stars") or 0)
    except Exception:
        stars = 0
    if not (1 <= stars <= 5):
        return JsonResponse({"ok": False, "error": "stars_out_of_range"}, status=400)

    comment = (data.get("comment") or "").strip()
    rev_id = data.get("id")

    # single, collision-proof upsert
    with transaction.atomic():
        # Prefer the current ticket's review (enforces one-per-ticket)
        r = (Review.objects.select_for_update().filter(ticket_link=tl).first())

        if not r and rev_id:
            # If the UI sent an existing review id (e.g., fallback one), reuse it
            r = (Review.objects
                    .select_for_update()
                    .filter(id=int(rev_id), member__customer__user=request.user)
                    .first())

        if not r:
            r = Review(ticket_link=tl, restaurant=tl.restaurant, member=tl.member)

        # Keep associations in sync no matter what came in
        r.ticket_link = tl
        r.restaurant  = tl.restaurant
        r.member      = tl.member
        r.stars       = stars
        r.comment     = comment
        r.save()

    return JsonResponse({
        "ok": True,
        "review": {"id": r.id, "stars": r.stars, "comment": r.comment or "", "updated_at": r.updated_at.isoformat()}
    })
def _owned_ticketlink(request, tl_id: int):
    """Return TicketLink if it belongs to the signed-in user, else None."""
    if not request.user.is_authenticated:
        return None
    return (TicketLink.objects
            .select_related("member", "member__customer", "restaurant")
            .filter(id=int(tl_id), member__customer__user=request.user)
            .first())

# -------- Review helpers --------
def _serialize_review(r: Review) -> dict:
    return {
        "id": r.id,
        "ticket_link_id": r.ticket_link_id,
        "restaurant_id": r.restaurant_id,
        "member_id": r.member_id,
        "stars": r.stars,
        "comment": r.comment or "",
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }

@require_http_methods(["POST"])
@csrf_protect
@login_required
def api_review_upsert(request: HttpRequest) -> JsonResponse:
    """
    Create or update a review.

    Body JSON:
      {
        "id": <optional int>,             # send to edit an existing review
        "ticket_link_id": <required int>, # required for create (ignored on edit)
        "stars": 1..5,                    # required
        "comment": "..."                  # optional
      }
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    review_id = data.get("id")
    tl_id = data.get("ticket_link_id")
    stars = data.get("stars")
    comment = (data.get("comment") or "").strip()

    # validate stars early
    try:
        stars = int(stars)
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid_stars"}, status=400)
    if stars < 1 or stars > 5:
        return JsonResponse({"ok": False, "error": "invalid_stars"}, status=400)

    # UPDATE path (when id provided)
    if review_id:
        rev = (
            Review.objects
            .select_related("ticket_link__member__customer", "restaurant", "member")
            .filter(pk=int(review_id))
            .first()
        )
        if not rev:
            return JsonResponse({"ok": False, "error": "review_not_found"}, status=404)

        # ownership: the ticket/member on the review must belong to this user
        owner = getattr(rev.ticket_link, "member", None)
        if not owner or getattr(owner.customer, "user", None) != request.user:
            return JsonResponse({"ok": False, "error": "not_authorized_for_review"}, status=403)

        rev.stars = stars
        rev.comment = comment
        rev.save(update_fields=["stars", "comment", "updated_at"])
        return JsonResponse({"ok": True, "review": _serialize_review(rev)})

    # CREATE / UPSERT path (no id => for this ticket)
    if not tl_id:
        return JsonResponse({"ok": False, "error": "missing_ticket_link_id"}, status=400)

    tl = (
        TicketLink.objects
        .select_related("member__customer", "restaurant")
        .filter(pk=int(tl_id), member__customer__user=request.user)
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "not_authorized_for_ticket"}, status=403)

    # get-or-create by unique ticket_link; if exists, we treat this as an update
    rev, created = Review.objects.get_or_create(
        ticket_link=tl,
        defaults={
            "restaurant": tl.restaurant,
            "member": tl.member,
            "stars": stars,
            "comment": comment,
        },
    )
    if not created:
        rev.stars = stars
        rev.comment = comment
        rev.restaurant = rev.restaurant or tl.restaurant
        rev.member = rev.member or tl.member
        rev.save(update_fields=["stars", "comment", "restaurant", "member", "updated_at"])

    return JsonResponse({"ok": True, "review": _serialize_review(rev)})
