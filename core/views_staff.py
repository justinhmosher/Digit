# core/views_staff.py
from __future__ import annotations

import json
from datetime import timedelta

from decouple import config
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import JsonResponse, HttpRequest
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import Member, TicketLink, RestaurantProfile
from .omnivore import (
    list_open_tickets,
    get_ticket,
    get_ticket_items,
    create_payment_with_tender_type,
)
from .utils import send_sms

from .views_processing import charge_customer_off_session, refund_payment_intent, PaymentError, build_idem_key

# ---------- Config ----------
LOCATION_ID = config("OMNIVORE_LOCATION_ID", default="").strip()
AUTO_TIP_PCT = float(config("AUTO_TIP_PCT", default="18"))  # staff close uses this, e.g. 18 for 18%

# ---------- Helpers ----------
def _emp_name(ticket: dict) -> str:
    emp = ((ticket or {}).get("_embedded") or {}).get("employee") or {}
    return emp.get("check_name") or (" ".join([emp.get("first_name",""), emp.get("last_name","")]).strip())

def _due_cents(ticket: dict) -> int:
    totals = (ticket or {}).get("totals") or {}
    return int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0

def _rp_for_location(loc_id: str) -> RestaurantProfile | None:
    rp = RestaurantProfile.objects.filter(omnivore_location_id=loc_id).first()
    if not rp:
        # fallback: single restaurant installs often have one row
        rp = RestaurantProfile.objects.first()
    return rp

def _create_pending_row(member: Member, loc_id: str, ticket_id: str) -> TicketLink:
    """
    Ensure there's a PENDING TicketLink visible on the board as soon as an invite is sent.
    Idempotent per (member, restaurant, ticket, status='pending').
    """
    rp = _rp_for_location(loc_id)
    try:
        t = get_ticket(loc_id, ticket_id)
    except Exception:
        t = {}

    server = _emp_name(t)
    ticket_no = t.get("ticket_number") or t.get("number") or ""
    due = _due_cents(t)

    tl, _ = TicketLink.objects.get_or_create(
        member=member,
        restaurant=rp,
        ticket_id=str(ticket_id),
        status="pending",
        defaults={
            "server_name": server or "",
            "last_total_cents": int(due or 0),
            "ticket_number": ticket_no or "",
        },
    )
    return tl

# ---------- UI ----------
@ensure_csrf_cookie
@login_required
@require_http_methods(["GET"])
def staff_console(request: HttpRequest):
    return render(request, "core/staff_console.html", {"auto_tip_pct": int(AUTO_TIP_PCT)})

# ---------- APIs ----------

@login_required
@require_GET
def api_staff_board_state(request: HttpRequest):
    """
    Returns board state:
      - pending: one entry per pending TicketLink
      - open:    grouped by ticket_id; shows members and last due
      - closed:  last 12h, one entry per TicketLink
    """
    now = timezone.now()
    cutoff = now - timedelta(hours=12)

    # PENDING
    pending_qs = (
        TicketLink.objects
        .select_related("member", "restaurant")
        .filter(status="pending")
        .order_by("-opened_at")[:200]
    )
    pending = []
    for tl in pending_qs:
        pending.append({
            "ticket_link_id": tl.id,
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number or "",
            "table": tl.table or "",
            "server": tl.server_name or "",
            "member": tl.member.number,
            "member_last": tl.member.last_name,
            "opened_ago": timesince(tl.opened_at) + " ago",
        })

    # OPEN (grouped)
    open_qs = (
        TicketLink.objects
        .select_related("member", "restaurant")
        .filter(status="open")
        .order_by("-opened_at")[:200]
    )
    open_map: dict[str, dict] = {}
    for tl in open_qs:
        bucket = open_map.setdefault(tl.ticket_id, {
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number or "",
            "table": tl.table or "",
            "server": tl.server_name or "",
            "members": [],
            "due_cents": int(tl.last_total_cents or 0),
        })
        bucket["members"].append(tl.member.number)
        # keep the largest last_total snapshot
        bucket["due_cents"] = max(bucket["due_cents"], int(tl.last_total_cents or 0))
    open_list = list(open_map.values())

    # CLOSED (last 12h)
    closed_qs = (
        TicketLink.objects
        .select_related("member", "restaurant")
        .filter(status="closed", closed_at__gte=cutoff)
        .order_by("-closed_at")[:200]
    )
    closed = []
    for tl in closed_qs:
        closed.append({
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number or "",
            "member": tl.member.number,
            "member_last": tl.member.last_name,
            "server": tl.server_name or "",
            "closed_ago": timesince(tl.closed_at) + " ago" if tl.closed_at else "",
        })

    return JsonResponse({"ok": True, "pending": pending, "open": open_list, "closed": closed})


@login_required
@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_link_member_to_ticket(request: HttpRequest):
    """
    Body: { member_number, last_name?, check_hint? OR ticket_id? }
    SMS the verification link AND create a PENDING TicketLink immediately so it
    appears on the board. If multiple open tickets match, return a selector.
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    member_number = (data.get("member_number") or "").strip()
    last_name     = (data.get("last_name") or "").strip()
    check_hint    = (data.get("check_hint") or "").strip()
    ticket_id     = (data.get("ticket_id") or "").strip()

    # 1) member
    m = Member.objects.filter(number=member_number).select_related("customer").first()
    if not m or (last_name and m.last_name.lower() != last_name.lower()):
        return JsonResponse({"ok": False, "error": "member_not_found_or_last_name_mismatch"}, status=404)

    # 2) ticket
    def _match_ticket_hint(t: dict, hint: str) -> bool:
        if not hint:
            return True
        h = str(hint).lower()
        if h == str(t.get("id")).lower():
            return True
        if h == str(t.get("ticket_number")).lower():
            return True
        # human fields
        txt = " ".join(str(t.get(k, "")).lower() for k in ("name", "table"))
        emp = ((t.get("_embedded") or {}).get("employee") or {})
        txt += " " + " ".join(str(emp.get(k,"")).lower() for k in ("check_name","first_name","last_name"))
        return h in txt

    chosen_id = ""
    if ticket_id:
        try:
            t = get_ticket(LOCATION_ID, ticket_id)
        except Exception:
            return JsonResponse({"ok": False, "error": "ticket_not_found"}, status=404)
        if not t.get("open", True):
            return JsonResponse({"ok": False, "error": "ticket_not_open"}, status=400)
        chosen_id = str(t.get("id"))
    else:
        cands = list_open_tickets(LOCATION_ID)
        hits = [tt for tt in cands if _match_ticket_hint(tt, check_hint)]
        if not hits:
            return JsonResponse({"ok": False, "error": "no_open_ticket_match"}, status=404)
        if len(hits) > 1:
            return JsonResponse({
                "ok": True, "multiple": True,
                "candidates": [
                    {
                        "ticket_id": tt.get("id"),
                        "label": (
                            str(tt.get("ticket_number") or "") or
                            _emp_name(tt) or
                            str(tt.get("id"))
                        ),
                    } for tt in hits
                ],
            })
        chosen_id = str(hits[0].get("id"))

    # 3) signed verify link
    token = signing.TimestampSigner().sign_object({"m": m.number, "loc": LOCATION_ID, "ticket": chosen_id})
    verify_path = reverse("core:verify_member", args=[m.number])
    verify_url  = request.build_absolute_uri(f"{verify_path}?t={token}")

    # 4) send SMS
    phone = getattr(getattr(m, "customer", None), "phone", "") or ""
    if not phone:
        return JsonResponse({"ok": False, "error": "member_has_no_phone"}, status=400)
    body = "Dine N Dash: Tap to verify your visit, then enter your 4-digit PIN.\n" + verify_url
    sent = send_sms(phone, body)

    # 5) create / ensure PENDING row appears
    tl = _create_pending_row(m, LOCATION_ID, chosen_id)

    return JsonResponse({"ok": True, "sent": sent, "ticket_link_id": tl.id})


@login_required
@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_staff_resend_link(request: HttpRequest):
    """
    Body: { ticket_link_id }
    Resend verification SMS for a PENDING TicketLink.
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    tl_id = data.get("ticket_link_id")
    tl = TicketLink.objects.select_related("member", "member__customer", "restaurant").filter(id=tl_id, status="pending").first()
    if not tl:
        return JsonResponse({"ok": False, "error": "pending_link_not_found"}, status=404)

    token = signing.TimestampSigner().sign_object({"m": tl.member.number, "loc": tl.restaurant.omnivore_location_id or LOCATION_ID, "ticket": tl.ticket_id})
    verify_url  = request.build_absolute_uri(f"{reverse('core:verify_member', args=[tl.member.number])}?t={token}")

    phone = getattr(getattr(tl.member, "customer", None), "phone", "") or ""
    if not phone:
        return JsonResponse({"ok": False, "error": "member_has_no_phone"}, status=400)
    body = "Dine N Dash: Tap to verify your visit, then enter your 4-digit PIN.\n" + verify_url
    sent = send_sms(phone, body)
    return JsonResponse({"ok": True, "sent": sent})


@login_required
@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_staff_cancel_link(request: HttpRequest):
    """
    Body: { ticket_link_id }
    Cancel a PENDING invite (delete the row to avoid unique constraint collisions).
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    tl_id = data.get("ticket_link_id")
    tl = TicketLink.objects.filter(id=tl_id, status="pending").first()
    if not tl:
        return JsonResponse({"ok": False, "error": "pending_link_not_found"}, status=404)

    tl.delete()
    return JsonResponse({"ok": True})


# core/views_staff.py (or wherever your close endpoint lives)

from decimal import Decimal
from django.utils import timezone
from decouple import config
import stripe

# assumes stripe.api_key is already set globally

# Optional: read a platform fee percent from env, e.g. 5 = 5%. Default 0 (no fee).
_PLATFORM_FEE_PCT = Decimal(config("PLATFORM_FEE_PCT", default="0"))  # e.g. "5" for 5%

@ensure_csrf_cookie
@csrf_protect
@require_POST
@login_required
def api_staff_close_ticket(request: HttpRequest):
    """
    Body: { ticket_id, reference? }
    Mirrors customer close:
      - base_due = subtotal + tax
      - auto tip = AUTO_TIP_PCT * base_due
      - Stripe destination charge for base+tip
      - POS post (amount=base, tip=tip) with fallback to (amount=base+tip, tip=0)
      - Snapshot normalized items + totals into all open TicketLinks
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    ticket_id = (data.get("ticket_id") or "").strip()
    reference = (data.get("reference") or "staff-close").strip()
    if not ticket_id:
        return JsonResponse({"ok": False, "error": "missing_ticket_id"}, status=400)

    # Gather all open links for this ticket
    links = list(
        TicketLink.objects
        .select_related("restaurant", "member", "member__customer")
        .filter(ticket_id=ticket_id, status="open")
        .order_by("opened_at")
    )
    if not links:
        return JsonResponse({"ok": False, "error": "no_open_links_for_ticket"}, status=404)

    rp: RestaurantProfile = links[0].restaurant

    if not getattr(rp, "stripe_account_id", ""):
        return JsonResponse({"ok": False, "error": "restaurant_missing_stripe_connect_account"}, status=400)

    location_id = (rp.omnivore_location_id or LOCATION_ID or "").strip()
    if not location_id:
        return JsonResponse({"ok": False, "error": "restaurant_missing_location_id"}, status=500)

    # Fresh ticket JSON
    try:
        ticket_json = get_ticket(location_id, ticket_id) or {}
    except Exception:
        ticket_json = {}

    # Authoritative base = subtotal + tax
    base_due = _compute_base_due(ticket_json)

    if base_due <= 0:
        base_due = max([int(l.last_total_cents or 0) for l in links] or [0])
    if base_due <= 0:
        return JsonResponse({"ok": False, "error": "nothing_due"}, status=400)

    # Auto tip (18% default) on the base_due
    tip_cents = int(round((AUTO_TIP_PCT / 100.0) * base_due))
    gross_cents = base_due + tip_cents

    # Billable customer (take the first link with a member->customer)
    member = next((lk.member for lk in links if getattr(lk, "member", None)), None)
    customer_profile = getattr(member, "customer", None) if member else None
    if not customer_profile:
        return JsonResponse({"ok": False, "error": "no_customer_for_ticket"}, status=400)
    if not customer_profile.stripe_customer_id or not customer_profile.default_payment_method:
        return JsonResponse({"ok": False, "error": "customer_missing_payment_method"}, status=400)

    # Optional platform fee
    app_fee_cents = 0
    if _PLATFORM_FEE_PCT > 0:
        app_fee_cents = int((Decimal(gross_cents) * _PLATFORM_FEE_PCT / Decimal(100)).quantize(Decimal("1")))

    description = f"Dine N Dash — Ticket {ticket_id} ({rp.display_name()})"
    metadata = {
        "ticket_id": ticket_id,
        "restaurant_id": str(rp.id),
        "customer_profile_id": str(customer_profile.id),
        "source": "staff_close",
        "auto_tip_pct": str(AUTO_TIP_PCT),
    }

    idem_key = build_idem_key("staff_close", {
        "ticket_id": ticket_id,
        "restaurant": rp.id,
        "gross": gross_cents,
        "tip": tip_cents,
        "pm": customer_profile.default_payment_method,
    })

    # Charge (destination charge)
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(gross_cents),
            currency="usd",
            customer=customer_profile.stripe_customer_id,
            payment_method=customer_profile.default_payment_method,
            payment_method_types=["card"],
            confirm=True,
            off_session=True,
            description=description,
            metadata=metadata,
            transfer_data={"destination": rp.stripe_account_id},
            application_fee_amount=app_fee_cents or None,
            on_behalf_of=rp.stripe_account_id,
            idempotency_key=idem_key,
        )
    except stripe.error.CardError as e:
        err = e.error
        return JsonResponse({
            "ok": False,
            "error": "stripe_charge_failed",
            "detail": err.message,
            "code": getattr(err, "code", None),
            "decline_code": getattr(err, "decline_code", None),
            "payment_intent": (getattr(err, "payment_intent", {}) or {}).get("id"),
        }, status=402)
    except stripe.error.StripeError as e:
        return JsonResponse({"ok": False, "error": "stripe_api_error", "detail": str(e)}, status=502)

    # POS post with fallback (amount=base, tip=tip) → else (amount=base+tip, tip=0)
    try:
        try:
            create_payment_with_tender_type(
                location_id=location_id,
                ticket_id=ticket_id,
                amount_cents=base_due,
                tender_type_id=None,
                reference=reference,
                tip_cents=tip_cents,
            )
        except Exception:
            create_payment_with_tender_type(
                location_id=location_id,
                ticket_id=ticket_id,
                amount_cents=base_due + tip_cents,
                tender_type_id=None,
                reference=reference,
                tip_cents=0,
            )
    except Exception as pos_err:
        # Refund if POS write failed
        try:
            if getattr(intent, "id", None):
                stripe.Refund.create(payment_intent=intent.id, reason="requested_by_customer")
        except stripe.error.StripeError as refund_err:
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
            "detail": str(pos_err),
            "payment_intent": getattr(intent, "id", None),
        }, status=502)

    # Snapshot items & totals like customer flow
    try:
        normalized_items = _normalize_line_items(ticket_json) if ticket_json else []
    except Exception:
        normalized_items = []

    try:
        totals_from_pos = _totals_from_ticket(ticket_json) if ticket_json else {}
    except Exception:
        totals_from_pos = {}

    now = timezone.now()
    update_fields = [
        "status", "closed_at",
        "total_cents", "tip_cents", "paid_cents",
        "discounts_cents", "subtotal_cents", "tax_cents",
        "items_json", "raw_ticket_json", "pos_ref",
        "merchant_name","merchant_addr1","merchant_addr2",
        "merchant_city","merchant_state","merchant_zip","merchant_phone",
    ]

    for link in links:
        _fill_merchant_snapshot(link, rp)
        link.status         = "closed"
        link.closed_at      = now
        link.subtotal_cents = int(totals_from_pos.get("subtotal_cents") or 0)
        link.tax_cents      = int(totals_from_pos.get("tax_cents") or 0)
        link.discounts_cents= int(totals_from_pos.get("discounts_cents") or 0)

        # Authoritative amounts
        link.total_cents    = int(base_due)         # base (subtotal + tax)
        link.tip_cents      = int(tip_cents or 0)
        link.paid_cents     = int(gross_cents or 0)

        # Preserve the items + raw for receipts
        link.items_json      = normalized_items or link.items_json or []
        link.raw_ticket_json = ticket_json or link.raw_ticket_json or {}

        link.pos_ref = reference
        link.save(update_fields=update_fields)

    return JsonResponse({
        "ok": True,
        "paid_cents": gross_cents,
        "auto_tip_cents": tip_cents,
        "base_due_cents": base_due,
        "payment_intent": getattr(intent, "id", None),
        "destination_account": rp.stripe_account_id,
        "application_fee_cents": app_fee_cents,
    })




@require_GET
def api_ticket_receipt(request, member):
    tl = (
        TicketLink.objects
        .filter(member__number=member, status="open")
        .order_by("-opened_at")
        .select_related("restaurant")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    loc_id = tl.restaurant.omnivore_location_id or ""
    t = get_ticket(loc_id, tl.ticket_id)
    items = get_ticket_items(loc_id, tl.ticket_id)

    subtotal_calc = 0
    for i in items:
        qty = int(i.get("quantity", 1) or 1)
        price = int(i.get("price", 0) or 0)
        subtotal_calc += qty * price

    totals = (t or {}).get("totals") or {}
    subtotal = int(totals.get("sub_total", subtotal_calc) or subtotal_calc)
    tax      = int(totals.get("tax", 0) or 0)
    total    = int(totals.get("total", subtotal + tax) or (subtotal + tax))
    due      = int(totals.get("due", total) or total)

    # remember what we last showed
    tl.last_total_cents = due
    tl.save(update_fields=["last_total_cents"])

    return JsonResponse({
        "ok": True,
        "ticket_id": tl.ticket_id,
        "server": tl.server_name,
        "items": [
            {
                "name": i.get("name"),
                "qty": int(i.get("quantity", 1) or 1),
                "cents": int(i.get("price", 0) or 0),
            } for i in items
        ],
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": total,
        "due_cents": due,
    })

# ---- Normalizers to mirror customer close-out ----
def _get_embedded_list(obj: dict, key: str) -> list:
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
        items.append({
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
        })
    return items

def _totals_from_ticket(ticket_json: dict) -> dict:
    totals = (ticket_json or {}).get("totals") or {}
    to_int = lambda v: int(v) if v not in (None, "") else 0
    sub_val = totals.get("subtotal", totals.get("sub_total", 0))
    return {
        "subtotal_cents":        to_int(sub_val),
        "tax_cents":             to_int(totals.get("tax")),
        "discounts_cents":       to_int(totals.get("discounts") or totals.get("discount") or 0),
        "total_cents":           to_int(totals.get("total")),
        "due_cents":             to_int(totals.get("due")),
        "service_charge_cents":  to_int(totals.get("service_charge") or totals.get("svc_charge") or 0),
    }

def _compute_base_due(ticket_json: dict) -> int:
    """Base = subtotal + tax. If missing, sum items."""
    to_int = lambda v: int(v) if v not in (None, "") else 0
    totals = (ticket_json or {}).get("totals") or {}
    sub = to_int(totals.get("sub_total", totals.get("subtotal", 0)))
    tax = to_int(totals.get("tax"))
    if sub <= 0:
        sub_calc = 0
        for li in _get_embedded_list(ticket_json, "items") or _get_embedded_list(ticket_json, "line_items"):
            qty  = int(li.get("quantity") or 1)
            unit = int(li.get("price") or li.get("price_per_unit") or li.get("unit_price") or 0)
            if unit:
                sub_calc += qty * unit
            else:
                sub_calc += int(li.get("total") or 0)
        sub = sub_calc
    return max(sub + tax, 0)

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
