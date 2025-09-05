# core/views_staff.py
from django.shortcuts import render
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from decouple import config
import json

from .models import Member, TicketLink
from .omnivore import (
    list_open_tickets,
    get_ticket,
    get_ticket_items,
    create_external_payment,
)

LOCATION_ID = config("OMNIVORE_LOCATION_ID")


# ---------------- UI ----------------

@ensure_csrf_cookie          # set csrftoken cookie for the page
@require_http_methods(["GET"])
def staff_console(request):
    return render(request, "core/staff_console.html", {})


# ------------- Helpers --------------

def _emp_name(ticket: dict) -> str:
    """Best-effort server name from HAL: _embedded.employee."""
    emp = ((ticket or {}).get("_embedded") or {}).get("employee") or {}
    return emp.get("check_name") or (" ".join([emp.get("first_name",""), emp.get("last_name","")]).strip())


def _due_cents(ticket: dict) -> int:
    """Amount still due on the ticket (integer cents)."""
    totals = (ticket or {}).get("totals") or {}
    # Omnivore exposes 'due' when there are partial payments; fall back to 'total'
    return int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0


def _match_ticket_hint(t: dict, hint: str) -> bool:
    """Human-friendly match. Supports id, ticket_number, and the prebuilt 'match_text' from list_open_tickets()."""
    if not hint:
        return True
    hint = str(hint).lower()
    if hint == str(t.get("id")).lower():
        return True
    if hint == str(t.get("ticket_number")).lower():
        return True
    if hint in (t.get("match_text") or ""):
        return True
    # fallbacks on a few visible fields
    txt = " ".join(str(t.get(k, "")).lower() for k in ("name", "table", "guest_name"))
    return hint in txt


# ------------- APIs -----------------

@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_link_member_to_ticket(request):
    """
    Link a member to an open POS ticket.

    POST JSON:
      - member_number (required)
      - last_name (optional human confirmation)
      - check_hint (optional: table name, partial ticket number, server check name)
      - ticket_id (optional: if front-end already chose exact ticket)

    Responses:
      { ok, ticket_id, server_name, member_last }               # success
      { ok: True, multiple: True, candidates: [{ticket_id,label}, ...] }  # need staff to choose
      404 with {error: "..."} if no member or no open ticket matched
    """
    data = json.loads(request.body.decode() or "{}")

    member_number = (data.get("member_number") or "").strip()
    last_name     = (data.get("last_name") or "").strip()
    check_hint    = (data.get("check_hint") or "").strip()
    ticket_id     = (data.get("ticket_id") or "").strip()

    # validate member
    m = Member.objects.filter(number=member_number).first()
    if not m or (last_name and m.last_name.lower() != last_name.lower()):
        return JsonResponse({"ok": False, "error": "Member not found or last name mismatch."}, status=404)

    # if ticket_id provided, short-circuit search
    ticket = None
    if ticket_id:
        try:
            ticket = get_ticket(LOCATION_ID, ticket_id)
        except Exception:
            return JsonResponse({"ok": False, "error": "Ticket not found."}, status=404)
        if not ticket.get("open", True):
            return JsonResponse({"ok": False, "error": "Ticket is not open."}, status=404)
    else:
        # search open tickets
        candidates = list_open_tickets(LOCATION_ID)  # already limited to open
        hits = [t for t in candidates if _match_ticket_hint(t, check_hint)]
        if not hits:
            return JsonResponse({"ok": False, "error": "No open check matched that hint."}, status=404)
        if len(hits) > 1:
            return JsonResponse({
                "ok": True,
                "multiple": True,
                "candidates": [
                    {
                        "ticket_id": t.get("id"),
                        "label": (
                            str(t.get("ticket_number") or "") or
                            _emp_name(t) or
                            t.get("name") or
                            str(t.get("id"))
                        ),
                    } for t in hits
                ],
            })
        ticket = hits[0]

    tl = TicketLink.objects.create(
        member=m,
        location_id=LOCATION_ID,
        ticket_id=str(ticket.get("id")),
        server_name=_emp_name(ticket) or "",
        last_total=_due_cents(ticket),
    )

    return JsonResponse({
        "ok": True,
        "ticket_id": tl.ticket_id,
        "server_name": tl.server_name,
        "member_last": m.last_name,
    })


@require_GET
def api_ticket_receipt(request, member_number):
    """
    Return a live receipt for the most recent *open* link for this member.
    """
    tl = (
        TicketLink.objects
        .filter(member__number=member_number, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    # fresh data from POS
    t = get_ticket(tl.location_id, tl.ticket_id)
    items = get_ticket_items(tl.location_id, tl.ticket_id)  # HAL -> list already

    # item rows: price is cents per line; multiply by quantity when present
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
    tl.last_total = due
    tl.save(update_fields=["last_total"])

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


@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_close_tab(request, member_number):
    """
    Close the linked ticket by recording an external payment equal to the current 'due'.
    Body: { reference: "your-charge-id" }
    """
    data = json.loads(request.body.decode() or "{}")
    reference = (data.get("reference") or "demo_txn").strip()

    tl = (
        TicketLink.objects
        .filter(member__number=member_number, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    t = get_ticket(tl.location_id, tl.ticket_id)
    amount = _due_cents(t) or tl.last_total or 0
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "Nothing due."}, status=400)

    # (1) In production: charge via Stripe/processor here and set 'reference' to the charge id.
    # (2) Reflect that payment on the POS as an EXTERNAL payment:
    create_external_payment(tl.location_id, tl.ticket_id, amount, reference)

    tl.status = "closed"
    tl.external_txn_id = reference
    tl.closed_at = timezone.now()
    tl.save(update_fields=["status", "external_txn_id", "closed_at"])

    return JsonResponse({"ok": True})
