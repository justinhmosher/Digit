# core/views.py
from __future__ import annotations

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, redirect, resolve_url
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.views.decorators.http import require_GET, require_POST

from decouple import config

from .models import CustomerProfile, Member, TicketLink
from .omnivore import (
    get_ticket,
    get_ticket_items,
    create_external_payment,
)

# If you also need list_open_tickets for staff flows, import it as well.
# from .omnivore import list_open_tickets

# ---------------------------
# Helpers
# ---------------------------

def _user_has_customer(user) -> bool:
    return bool(getattr(user, "customer_profile", None))

def _member_for_user(user) -> Member | None:
    """Return the Member linked to the current user's CustomerProfile, if any."""
    if not user.is_authenticated:
        return None
    cp: CustomerProfile | None = getattr(user, "customer_profile", None)
    if not cp:
        return None
    return Member.objects.filter(customer=cp).first()

def _due_cents(ticket: dict) -> int:
    """Amount still due on the ticket (integer cents)."""
    totals = (ticket or {}).get("totals") or {}
    if totals.get("due") is not None:
        try:
            return int(totals.get("due") or 0)
        except Exception:
            pass
    try:
        return int(totals.get("total") or 0)
    except Exception:
        return 0

# ---------------------------
# Page view (matches your HTML)
# ---------------------------

@ensure_csrf_cookie  # sets csrftoken cookie so JS can POST close-tab
def customer_home(request: HttpRequest) -> HttpResponse:
    """
    Render the customer homepage with tabs.
    Context:
      - has_customer: bool
      - has_live_order: bool
      - member_number: str ('' if none)
    """
    has_customer = request.user.is_authenticated and _user_has_customer(request.user)
    member_number = ""
    has_live_order = False

    if has_customer:
        m = _member_for_user(request.user)
        if m:
            member_number = m.number
            has_live_order = TicketLink.objects.filter(member=m, status="open").exists()

    ctx = {
        "has_customer": has_customer,
        "has_live_order": has_live_order,
        "member_number": member_number,
    }
    return render(request, "core/homepage.html", ctx)

# ---------------------------
# Auth utility (used by your HTML)
# ---------------------------

def signout(request: HttpRequest) -> HttpResponse:
    """
    Log the user out and return to homepage (or ?next= safe local path).
    """
    logout(request)
    nxt = request.GET.get("next")
    # Only allow local relative redirects; otherwise send to home
    if nxt and nxt.startswith("/"):
        return redirect(nxt)
    return redirect(resolve_url("core:home"))

# ---------------------------
# Customer-only LIVE ORDER APIs
# ---------------------------

def _assert_member_ownership(request: HttpRequest, member_number: str) -> Member | None:
    """
    Ensure the current user owns the given member_number.
    Return the Member if valid; otherwise None.
    """
    if not request.user.is_authenticated:
        return None
    m = _member_for_user(request.user)
    if not m or m.number != str(member_number):
        return None
    return m

@require_GET
def api_ticket_receipt(request: HttpRequest, member_number: str) -> JsonResponse:
    """
    Return a live receipt for the most recent *open* TicketLink owned by this user.
    """
    m = _assert_member_ownership(request, member_number)
    if not m:
        return JsonResponse({"ok": False, "error": "Not authorized for this member."}, status=403)

    tl = (
        TicketLink.objects
        .filter(member=m, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    # Pull fresh details from Omnivore
    t = get_ticket(tl.location_id, tl.ticket_id)
    items = get_ticket_items(tl.location_id, tl.ticket_id)  # list

    # Compute item subtotal if POS doesn't provide (or as safety)
    subtotal_calc = 0
    rows = []
    for i in items:
        qty = int(i.get("quantity", 1) or 1)
        cents = int(i.get("price", 0) or 0)
        subtotal_calc += qty * cents
        rows.append({
            "name": i.get("name"),
            "qty": qty,
            "cents": cents,
        })

    totals = (t or {}).get("totals") or {}
    subtotal = int(totals.get("sub_total", subtotal_calc) or subtotal_calc)
    tax      = int(totals.get("tax", 0) or 0)
    total    = int(totals.get("total", subtotal + tax) or (subtotal + tax))
    due      = int(totals.get("due", total) or total)

    # remember last due we showed
    tl.last_total = due
    tl.save(update_fields=["last_total"])

    return JsonResponse({
        "ok": True,
        "ticket_id": tl.ticket_id,
        "server": tl.server_name,
        "items": rows,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": total,
        "due_cents": due,
    })

@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_close_tab(request: HttpRequest, member_number: str) -> JsonResponse:
    """
    Reflect an external payment equal to current 'due' and mark our link closed.
    Body JSON: { reference: "processor-charge-id" }
    """
    m = _assert_member_ownership(request, member_number)
    if not m:
        return JsonResponse({"ok": False, "error": "Not authorized for this member."}, status=403)

    tl = (
        TicketLink.objects
        .filter(member=m, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    # Pull the current due from POS
    t = get_ticket(tl.location_id, tl.ticket_id)
    amount = _due_cents(t) or tl.last_total or 0
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "Nothing due."}, status=400)

    # In production: charge via your processor and set reference to that charge id.
    import json
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}
    reference = (data.get("reference") or f"dine-nd-{timezone.now().timestamp():.0f}").strip()

    # Reflect as EXTERNAL payment on the POS
    create_external_payment(tl.location_id, tl.ticket_id, amount, reference)

    # Close our link
    tl.status = "closed"
    tl.external_txn_id = reference
    tl.closed_at = timezone.now()
    tl.save(update_fields=["status", "external_txn_id", "closed_at"])

    return JsonResponse({"ok": True})
