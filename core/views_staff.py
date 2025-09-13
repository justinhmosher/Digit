# c# core/views_staff.py
from __future__ import annotations

import json
from datetime import timedelta

from decouple import config
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .models import Member, TicketLink, StaffProfile, RestaurantProfile
from .omnivore import (
    list_open_tickets,
    get_ticket,
    get_ticket_items,
    create_payment_with_tender_type,
)
from .utils import send_sms


# ---------- helpers ----------

def _staff_restaurant(request) -> RestaurantProfile | None:
    sp = getattr(request.user, "staffprofile", None)
    return getattr(sp, "restaurant", None)

def _staff_location_id(request) -> str:
    rp = _staff_restaurant(request)
    if rp and getattr(rp, "omnivore_location_id", ""):
        return rp.omnivore_location_id.strip()
    # Dev fallback so you can still test before linking a restaurant
    return config("OMNIVORE_LOCATION_ID", default="").strip()

def _emp_name(ticket: dict) -> str:
    emp = ((ticket or {}).get("_embedded") or {}).get("employee") or {}
    return emp.get("check_name") or (" ".join([emp.get("first_name", ""), emp.get("last_name", "")]).strip())

def _due_cents(ticket: dict) -> int:
    totals = (ticket or {}).get("totals") or {}
    return int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0

def _match_ticket_hint(t: dict, hint: str) -> bool:
    if not hint:
        return True
    hint = str(hint).lower()
    if hint == str(t.get("id")).lower():
        return True
    if hint == str(t.get("ticket_number")).lower():
        return True
    if hint in (t.get("match_text") or ""):
        return True
    txt = " ".join(str(t.get(k, "")).lower() for k in ("name", "table", "guest_name"))
    return hint in txt


# ---------- UI ----------

@login_required
@ensure_csrf_cookie
@require_http_methods(["GET"])
def staff_console(request):
    """Light wrapper to render the staff console UI."""
    rp = _staff_restaurant(request)
    return render(request, "core/staff_console.html", {"restaurant": rp})


# ---------- APIs ----------

@login_required
@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_link_member_to_ticket(request):
    """
    Staff provides: member_number, optional last_name, and either ticket_id or check_hint.
    Sends the member a signed verification URL (no TicketLink is created until guest verifies).
    """
    data = json.loads(request.body.decode() or "{}")
    member_number = (data.get("member_number") or "").strip()
    last_name     = (data.get("last_name") or "").strip()
    check_hint    = (data.get("check_hint") or "").strip()
    ticket_id     = (data.get("ticket_id") or "").strip()

    if not member_number:
        return JsonResponse({"ok": False, "error": "Member number is required."}, status=400)

    m = Member.objects.filter(number=member_number).select_related("customer").first()
    if not m or (last_name and m.last_name.lower() != last_name.lower()):
        return JsonResponse({"ok": False, "error": "Member not found or last name mismatch."}, status=404)

    location_id = _staff_location_id(request)
    if not location_id:
        return JsonResponse({"ok": False, "error": "No POS location configured for this staff account."}, status=400)

    # Resolve a single open ticket
    if ticket_id:
        try:
            t = get_ticket(location_id, ticket_id)
        except Exception:
            return JsonResponse({"ok": False, "error": "Ticket not found."}, status=404)
        if not t.get("open", True):
            return JsonResponse({"ok": False, "error": "Ticket is not open."}, status=404)
        chosen_id = str(t.get("id"))
    else:
        cands = list_open_tickets(location_id)
        hits = [tt for tt in cands if _match_ticket_hint(tt, check_hint)]
        if not hits:
            return JsonResponse({"ok": False, "error": "No open check matched that hint."}, status=404)
        if len(hits) > 1:
            return JsonResponse({
                "ok": True,
                "multiple": True,
                "candidates": [
                    {
                        "ticket_id": tt.get("id"),
                        "label": str(tt.get("ticket_number") or "") or _emp_name(tt) or tt.get("name") or str(tt.get("id")),
                    }
                    for tt in hits
                ],
            })
        chosen_id = str(hits[0].get("id"))

    # Build verification link token (no TicketLink yet)
    token = signing.TimestampSigner().sign_object({
        "m": m.number,
        "loc": location_id,
        "ticket": chosen_id,
    })
    verify_path = reverse("core:verify_member", args=[m.number])
    verify_url  = request.build_absolute_uri(f"{verify_path}?t={token}")

    # Send SMS to member
    phone = getattr(getattr(m, "customer", None), "phone", "") or ""
    if not phone:
        return JsonResponse({"ok": False, "error": "Member has no phone on file."}, status=400)

    body = "Dine N Dash: Tap to verify your visit, then enter your 4-digit PIN.\n" + verify_url
    sent = send_sms(phone, body)

    return JsonResponse({"ok": True, "sent": sent})


# in core/views_staff.py

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



# in core/views_staff.py

def _due_cents(ticket: dict) -> int:
    totals = (ticket or {}).get("totals") or {}
    return int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0

@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_staff_close_ticket(request):
    """
    Body: { "ticket_id": "...", "tip_cents": <int>, "reference": "staff-close" }
    Closes the POS ticket and marks all open TicketLink rows for that ticket as closed.
    """
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    ticket_id = str(data.get("ticket_id") or "").strip()
    tip_cents = int(data.get("tip_cents") or 0)
    reference = (data.get("reference") or "staff-close").strip()

    if not ticket_id:
        return JsonResponse({"ok": False, "error": "missing_ticket_id"}, status=400)

    # Find all open links for this ticket
    links = list(
        TicketLink.objects
        .select_related("restaurant")
        .filter(ticket_id=ticket_id, status="open")
        .order_by("opened_at")
    )
    if not links:
        return JsonResponse({"ok": False, "error": "no_open_links_for_ticket"}, status=404)

    # All links must point to the same restaurant (single-location ticket)
    rp = links[0].restaurant
    location_id = (rp.omnivore_location_id or "").strip()
    if not location_id:
        return JsonResponse({"ok": False, "error": "restaurant_missing_location_id"}, status=500)

    # Get current due from POS; fall back to largest last_total_cents we have locally
    try:
        t = get_ticket(location_id, ticket_id)
        amount = _due_cents(t)
    except Exception:
        amount = max([l.last_total_cents or 0 for l in links] or [0])

    if amount <= 0:
        return JsonResponse({"ok": False, "error": "nothing_due"}, status=400)

    # Post the payment to Omnivore (your adapter posts CASH + TIP only)
    try:
        create_payment_with_tender_type(
            location_id=location_id,
            ticket_id=ticket_id,
            amount_cents=amount,
            tender_type_id=None,        # adapter ignores this
            reference=reference,        # not sent in adapter body, but we keep it for logging
            tip_cents=tip_cents,
        )
    except Exception as e:
        return JsonResponse({"ok": False, "error": "omnivore_payment_failed", "detail": str(e)}, status=502)

    # Mark all links as closed and snapshot simple totals
    now = timezone.now()
    for tl in links:
        tl.status = "closed"
        tl.closed_at = now
        # snapshot outcome
        tl.tip_cents = tip_cents
        tl.paid_cents = amount
        tl.pos_ref = reference
        tl.save(update_fields=["status", "closed_at", "tip_cents", "paid_cents", "pos_ref"])

    return JsonResponse({"ok": True})



@login_required
@require_GET
def api_staff_board_state(request):
    """
    Returns three lists:
      pending: TicketLink.status == 'pending'
      open:    TicketLink.status == 'open' (grouped by ticket)
      closed:  TicketLink.status == 'closed' AND closed_at within last 12h
    """
    now = timezone.now()
    cutoff = now - timedelta(hours=12)

    # PENDING
    pending_qs = (
        TicketLink.objects
        .select_related("member")
        .filter(status="pending")
        .order_by("-opened_at")[:100]
    )
    pending = []
    for tl in pending_qs:
        pending.append({
            "ticket_id": tl.ticket_id,
            "ticket_number": None,
            "table": None,
            "member": tl.member.number,
            "member_last": tl.member.last_name,
            "opened_ago": timesince(tl.opened_at) + " ago",
        })

    # OPEN (group by ticket)
    open_qs = (
        TicketLink.objects
        .select_related("member")
        .filter(status="open")
        .order_by("-opened_at")[:100]
    )
    open_map: dict[str, dict] = {}
    for tl in open_qs:
        open_map.setdefault(tl.ticket_id, {
            "ticket_id": tl.ticket_id,
            "ticket_number": None,
            "table": None,
            "server": tl.server_name or "",
            "members": [],
            "due_cents": getattr(tl, "last_total_cents", 0),
        })
        open_map[tl.ticket_id]["members"].append(tl.member.number)
    open_list = list(open_map.values())

    # CLOSED (last 12h)
    closed_qs = (
        TicketLink.objects
        .select_related("member")
        .filter(status="closed", closed_at__gte=cutoff)
        .order_by("-closed_at")[:100]
    )
    closed = []
    for tl in closed_qs:
        closed.append({
            "ticket_id": tl.ticket_id,
            "ticket_number": None,
            "member": tl.member.number,
            "member_last": tl.member.last_name,
            "server": tl.server_name or "",
            "closed_ago": timesince(tl.closed_at) + " ago" if tl.closed_at else "",
        })

    return JsonResponse({"ok": True, "pending": pending, "open": open_list, "closed": closed})
