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


@ensure_csrf_cookie
@csrf_protect
@require_POST
@login_required
def api_staff_close_ticket(request: HttpRequest):
    """
    Body: { ticket_id, reference? }

    Flow:
      1) Compute base and tip from current POS totals (fallback to snapshots).
      2) Identify the customer (via TicketLink.member.customer).
      3) Charge via Stripe off-session using saved PM (idempotent).
      4) Post payment to POS (Omnivore). If that fails, auto-refund Stripe.
      5) Close local TicketLinks and snapshot amounts.
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    ticket_id = (data.get("ticket_id") or "").strip()
    reference = (data.get("reference") or "staff-close").strip()
    if not ticket_id:
        return JsonResponse({"ok": False, "error": "missing_ticket_id"}, status=400)

    # 0) Gather TicketLinks for this ticket
    links = list(
        TicketLink.objects
        .select_related("restaurant", "member", "member__customer")
        .filter(ticket_id=ticket_id, status="open")
        .order_by("opened_at")
    )
    if not links:
        return JsonResponse({"ok": False, "error": "no_open_links_for_ticket"}, status=404)

    rp: RestaurantProfile = links[0].restaurant
    location_id = (rp.omnivore_location_id or LOCATION_ID or "").strip()
    if not location_id:
        return JsonResponse({"ok": False, "error": "restaurant_missing_location_id"}, status=500)

    # 1) Fresh totals from POS
    try:
        t = get_ticket(location_id, ticket_id)
    except Exception:
        t = {}

    totals    = (t or {}).get("totals") or {}
    subtotal  = int(totals.get("sub_total") or 0)
    tax       = int(totals.get("tax") or 0)
    base_amt  = int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0
    if base_amt <= 0:
        base_amt = max([int(l.last_total_cents or 0) for l in links] or [0])
    if base_amt <= 0:
        return JsonResponse({"ok": False, "error": "nothing_due"}, status=400)

    # 2) Compute tip + gross
    tip_cents  = int(round((AUTO_TIP_PCT / 100.0) * base_amt))
    paid_gross = base_amt + tip_cents

    # Find a billable customer from any link
    member = next((lk.member for lk in links if getattr(lk, "member", None)), None)
    customer_profile: CustomerProfile | None = getattr(member, "customer", None) if member else None
    if not customer_profile:
        return JsonResponse({"ok": False, "error": "no_customer_for_ticket"}, status=400)

    if not customer_profile.stripe_customer_id or not customer_profile.default_payment_method:
        return JsonResponse({"ok": False, "error": "customer_missing_payment_method"}, status=400)

    # Build exact Stripe payload (for deterministic idempotency)
    stripe_payload = {
        "amount": int(paid_gross),
        "currency": "usd",
        "customer": customer_profile.stripe_customer_id,
        "payment_method": customer_profile.default_payment_method,
        "payment_method_types": ["card"],
        "confirm": True,
        "off_session": True,
        "description": f"Dine N Dash — Ticket {ticket_id} ({rp.display_name()})",
        "metadata": {
            "ticket_id": ticket_id,
            "restaurant_id": str(rp.id),
            "customer_profile_id": str(customer_profile.id),
        },
    }
    idem_key = build_idem_key("close", stripe_payload)

    # 3) CHARGE (Stripe)
    try:
        intent = charge_customer_off_session(
            customer_id=customer_profile.stripe_customer_id,
            payment_method_id=customer_profile.default_payment_method,
            amount_cents=paid_gross,
            currency="usd",
            description=stripe_payload["description"],
            idempotency_key=idem_key,
            metadata=stripe_payload["metadata"],
        )
    except PaymentError as e:
        # Log & bubble rich info; UI can inspect JSON
        print("[Stripe FAIL]", e, "code=", e.code, "decline=", e.decline_code, "pi=", e.payment_intent_id)
        return JsonResponse({
            "ok": False,
            "error": "stripe_charge_failed",
            "detail": str(e),
            "code": e.code,
            "decline_code": e.decline_code,
            "payment_intent": e.payment_intent_id,
        }, status=402)

    # 4) POST TO POS; refund if it fails
    try:
        create_payment_with_tender_type(
            location_id=location_id,
            ticket_id=ticket_id,
            amount_cents=base_amt,
            tender_type_id=None,     # adapter ignores
            reference=reference,     # adapter ignores; kept for logs
            tip_cents=tip_cents,
        )
    except Exception as e:
        # Refund to avoid guest being charged without POS close
        try:
            if getattr(intent, "id", None):
                refund_payment_intent(intent.id, reason="requested_by_customer")
        except PaymentError as re:
            return JsonResponse({
                "ok": False,
                "error": "pos_post_failed_and_refund_failed",
                "pos_detail": str(e),
                "refund_detail": str(re),
                "payment_intent": getattr(intent, "id", None),
            }, status=502)

        return JsonResponse({
            "ok": False,
            "error": "omnivore_payment_failed_refunded",
            "detail": str(e),
            "payment_intent": getattr(intent, "id", None),
        }, status=502)

    # 5) CLOSE LOCALLY & snapshot
    now = timezone.now()
    for tl in links:
        tl.status = "closed"
        tl.closed_at = now
        tl.subtotal_cents = subtotal
        tl.tax_cents = tax
        tl.total_cents = base_amt
        tl.tip_cents = tip_cents
        tl.paid_cents = paid_gross
        tl.pos_ref = reference
        tl.save(update_fields=[
            "status","closed_at",
            "subtotal_cents","tax_cents","total_cents",
            "tip_cents","paid_cents","pos_ref",
        ])

    return JsonResponse({
        "ok": True,
        "paid_cents": paid_gross,
        "auto_tip_cents": tip_cents,
        "payment_intent": getattr(intent, "id", None),
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