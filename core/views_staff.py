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
    create_payment_with_tender_type
)
# add these imports near your other imports
from django.core import signing
from django.urls import reverse
from .utils import send_sms


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
    Staff provides: member_number, last_name (optional), and either ticket_id or check_hint.
    We find/resolve ONE open ticket, build a signed token with (member, ticket_id, tl=None),
    and send the customer a verification link via SMS.

    Response:
      { ok: True, sent: {ok, sid?, error?} } on success
      { ok: True, multiple: True, candidates: [...] } if multiple matches
      4xx with {error: "..."} on failure
    """
    data = json.loads(request.body.decode() or "{}")
    member_number = (data.get("member_number") or "").strip()
    last_name     = (data.get("last_name") or "").strip()
    check_hint    = (data.get("check_hint") or "").strip()
    ticket_id     = (data.get("ticket_id") or "").strip()

    # 1) validate member
    m = Member.objects.filter(number=member_number).select_related("customer").first()
    if not m or (last_name and m.last_name.lower() != last_name.lower()):
        return JsonResponse({"ok": False, "error": "Member not found or last name mismatch."}, status=404)

    # 2) resolve ticket
    if ticket_id:
        try:
            t = get_ticket(LOCATION_ID, ticket_id)
        except Exception:
            return JsonResponse({"ok": False, "error": "Ticket not found."}, status=404)
        if not t.get("open", True):
            return JsonResponse({"ok": False, "error": "Ticket is not open."}, status=404)
        chosen_id = str(t.get("id"))
    else:
        cands = list_open_tickets(LOCATION_ID)
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
                        "label": (
                            str(tt.get("ticket_number") or "") or
                            _emp_name(tt) or
                            tt.get("name") or
                            str(tt.get("id"))
                        ),
                    } for tt in hits
                ],
            })
        chosen_id = str(hits[0].get("id"))

    # 3) build verification link token (no TicketLink yet)
    token = signing.TimestampSigner().sign_object({
        "m": m.number,
        "loc": LOCATION_ID,
        "ticket": chosen_id,
        # you could include a nonce here if you want one-time enforcement
    })
    verify_path = reverse("core:verify_member", args=[m.number])
    verify_url  = request.build_absolute_uri(f"{verify_path}?t={token}")

    # 4) send SMS
    phone = getattr(getattr(m, "customer", None), "phone", "") or ""
    if not phone:
        return JsonResponse({"ok": False, "error": "Member has no phone on file."}, status=400)

    body = (
        "Dine N Dash: Tap to verify your visit, then enter your 4-digit PIN.\n"
        f"{verify_url}"
    )
    sent = send_sms(phone, body)

    return JsonResponse({"ok": True, "sent": sent})



@require_GET
def api_ticket_receipt(request, member):
    """
    Return a live receipt for the most recent *open* link for this member.
    """
    tl = (
        TicketLink.objects
        .filter(member__number=member, status="open")
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

# core/views_staff.py
from decouple import config
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
import json

from .models import TicketLink
from .omnivore import get_ticket, create_payment_with_tender_type

OMNIVORE_TENDER_TYPE_ID = config("OMNIVORE_TENDER_TYPE_ID", default="100")  # 100 = "3rd Party"

def _due_cents(ticket: dict) -> int:
    totals = (ticket or {}).get("totals") or {}
    return int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0


@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_close_tab(request, member):
    data = json.loads(request.body.decode() or "{}")
    reference = (data.get("reference") or "demo_txn").strip()
    tip_cents = int(data.get("tip_cents") or 0)   # <<--- read tip

    tl = (
        TicketLink.objects
        .filter(member__number=member, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    t = get_ticket(tl.location_id, tl.ticket_id)
    amount = _due_cents(t) or tl.last_total or 0
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "Nothing due."}, status=400)

    try:
        # For this adapter: send CASH with required tip; do NOT send name/reference/tender_type
        create_payment_with_tender_type(
            tl.location_id,
            tl.ticket_id,
            amount_cents=amount,
            tender_type_id=None,      # explicit: do not include tender_type
            reference=reference,      # we'll ignore this inside the poster
            tip_cents=tip_cents,      # pass the tip cents
        )
    except Exception as e:
        return JsonResponse(
            {"ok": False, "error": "omnivore_payment_failed", "detail": str(e)},
            status=502,
        )

    tl.status = "closed"
    tl.external_txn_id = reference
    tl.closed_at = timezone.now()
    tl.save(update_fields=["status", "external_txn_id", "closed_at"])

    return JsonResponse({"ok": True})

# --- add near other imports ---
from datetime import timedelta
from django.utils.timesince import timesince

# === Board page stays the same URL you already use ===
@ensure_csrf_cookie
@require_http_methods(["GET"])
def staff_console(request):
    # now just serves the new UI; no backend change needed here
    return render(request, "core/staff_console.html", {})

# === Board state API ===
@require_GET
def api_staff_board_state(request):
    """
    Returns three lists:
    - pending: TicketLink.status == 'pending'
    - open:    TicketLink.status == 'open'
    - closed:  TicketLink.status == 'closed' AND closed_at within 12h
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
            "ticket_number": None,   # could be filled by POS call if you want
            "table": None,           # same
            "member": tl.member.number,
            "member_last": tl.member.last_name,
            "opened_ago": timesince(tl.opened_at) + " ago",
        })

    # OPEN
    open_qs = (
        TicketLink.objects
        .select_related("member")
        .filter(status="open")
        .order_by("-opened_at")[:100]
    )
    # group by ticket_id so one card shows all members attached
    open_map = {}
    for tl in open_qs:
        open_map.setdefault(tl.ticket_id, {
            "ticket_id": tl.ticket_id,
            "ticket_number": None,
            "table": None,
            "server": tl.server_name or "",
            "members": [],
            "due_cents": tl.last_total or 0,
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



