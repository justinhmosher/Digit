# core/views_verify.py

from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import login as auth_login, get_backends
from django.contrib.auth.hashers import check_password

from .models import Member, TicketLink, RestaurantProfile
from .omnivore import get_ticket


def _due_from_ticket(t: dict) -> int:
    totals = (t or {}).get("totals") or {}
    if totals.get("due") is not None:
        return int(totals.get("due") or 0)
    return int(totals.get("total") or 0)


def _check_member_pin(member_obj: Member, raw_pin: str) -> bool:
    customer = getattr(member_obj, "customer", None)
    pin_hash = getattr(customer, "pin_hash", "") or ""
    return bool(pin_hash) and check_password(str(raw_pin), pin_hash)


def _safe_login(request, user):
    try:
        auth_login(request, user)
        return
    except Exception:
        pass
    backs = get_backends()
    backend_path = (
        f"{backs[0].__module__}.{backs[0].__class__.__name__}"
        if backs else "django.contrib.auth.backends.ModelBackend"
    )
    setattr(user, "backend", backend_path)
    auth_login(request, user)


def _redirect_to_ticket_or_profile():
    """
    Prefer a dedicated live-ticket page if you have one,
    otherwise fall back to profile.
    """
    try:
        return redirect(reverse("core:live_ticket"))
    except Exception:
        return redirect(reverse("core:profile"))


def verify_member(request, member):
    """
    Guest verification page (opened from the SMS).

    Behavior:
      - If a matching TicketLink is already OPEN:
            * auto-login, refresh quick POS fields, redirect to live ticket (no PIN)
      - Else (PENDING):
            * GET -> show PIN page
            * POST -> check PIN, flip PENDING->OPEN, auto-login, redirect
    """
    token = request.GET.get("t", "") or request.POST.get("t", "")
    if not token:
        return HttpResponseBadRequest("Missing token.")

    # Unsign & validate token (30 min)
    try:
        data = signing.TimestampSigner().unsign_object(token, max_age=60 * 60 * 6)
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid or expired link.")

    if str(data.get("m")) != str(member):
        return HttpResponseBadRequest("Token mismatch.")

    loc_id    = str(data.get("loc") or "")
    ticket_id = str(data.get("ticket") or "")
    if not (loc_id and ticket_id):
        return HttpResponseBadRequest("Malformed token.")

    # Load member + user
    try:
        m = Member.objects.select_related("customer", "customer__user").get(number=member)
    except Member.DoesNotExist:
        return HttpResponseBadRequest("Member not found.")
    user = getattr(getattr(m, "customer", None), "user", None)

    # Restaurant must exist
    rp = RestaurantProfile.objects.filter(omnivore_location_id=loc_id).first()
    if not rp:
        return HttpResponseBadRequest("Restaurant not wired to POS.")

    # --- Fast-path: already OPEN? (No PIN required) ----------------------------
    tl_open = (
        TicketLink.objects
        .filter(member=m, restaurant=rp, ticket_id=ticket_id, status="open")
        .order_by("-opened_at")
        .first()
    )
    if request.method == "GET" and tl_open:
        # Optional: refresh quick POS fields to keep the link current
        try:
            t = get_ticket(loc_id, ticket_id)
            if not t.get("open", True):
                return HttpResponseBadRequest("This ticket is no longer open.")
            server_name = ((t.get("_embedded") or {}).get("employee") or {}).get("check_name", "") or ""
            due_cents   = _due_from_ticket(t)
            ticket_no   = t.get("ticket_number") or t.get("number") or ""
            tl_open.server_name = server_name
            tl_open.last_total_cents = due_cents
            tl_open.ticket_number = ticket_no
            tl_open.save(update_fields=["server_name", "last_total_cents", "ticket_number"])
        except Exception:
            # If POS is briefly unavailable, don't block auto-login; user can still view cached ticket.
            pass

        if user:
            _safe_login(request, user)
        return _redirect_to_ticket_or_profile()
    # ---------------------------------------------------------------------------

    # If not already open, either render PIN page (GET) or validate PIN (POST)
    if request.method == "GET":
        # Ensure there's at least a pending link row (optional; okay to omit)
        tl_pending = (
            TicketLink.objects
            .filter(member=m, restaurant=rp, ticket_id=ticket_id, status="pending")
            .order_by("-opened_at")
            .first()
        )
        if not tl_pending:
            # Create a pending row so analytics and state are consistent
            TicketLink.objects.get_or_create(
                member=m, restaurant=rp, ticket_id=ticket_id, status="pending",
                defaults={"opened_at": timezone.now()}
            )
        return render(request, "core/verify_member.html", {"member": member, "token": token})

    # POST -> PIN validation & flip to OPEN
    pin = (request.POST.get("pin") or "").strip()
    if not _check_member_pin(m, pin):
        return render(
            request,
            "core/verify_member.html",
            {"member": member, "token": token, "error": "Incorrect PIN. Try again."},
        )

    # Confirm ticket still open in POS & pull quick fields
    try:
        t = get_ticket(loc_id, ticket_id)
    except Exception:
        return HttpResponseBadRequest("Ticket not found or POS unavailable.")

    if not t.get("open", True):
        return HttpResponseBadRequest("This ticket is no longer open.")

    server_name = ((t.get("_embedded") or {}).get("employee") or {}).get("check_name", "") or ""
    due_cents   = _due_from_ticket(t)
    ticket_no   = t.get("ticket_number") or t.get("number") or ""

    # Flip PENDING -> OPEN (or create OPEN if missing)
    tl = (
        TicketLink.objects
        .filter(member=m, restaurant=rp, ticket_id=ticket_id, status="pending")
        .order_by("-opened_at")
        .first()
    )
    if tl:
        tl.status = "open"
        tl.server_name = server_name
        tl.last_total_cents = due_cents
        tl.ticket_number = ticket_no
        tl.save(update_fields=["status", "server_name", "last_total_cents", "ticket_number"])
    else:
        TicketLink.objects.create(
            member=m,
            restaurant=rp,
            ticket_id=ticket_id,
            status="open",
            server_name=server_name,
            last_total_cents=due_cents,
            ticket_number=ticket_no,
            opened_at=timezone.now(),
        )

    # Auto-login & redirect to the live ticket
    if user:
        _safe_login(request, user)
    return _redirect_to_ticket_or_profile()
