# core/views_verify.py
from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest, JsonResponse
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import login
from django.contrib.auth.hashers import check_password

from .models import Member, TicketLink, RestaurantProfile
from .omnivore import get_ticket
from django.contrib.auth import login as auth_login
from django.contrib.auth.hashers import check_password

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
    """
    Compare a 4-digit PIN to the hashed value stored on the related CustomerProfile.pin_hash.
    """
    customer = getattr(member_obj, "customer", None)
    pin_hash = getattr(customer, "pin_hash", "") or ""
    return bool(pin_hash) and check_password(str(raw_pin), pin_hash)


def _safe_login(request, user):
    """
    Log the user in even if no backend is attached to the user instance.
    """
    try:
        auth_login(request, user)  # works if request already knows the backend
        return
    except Exception:
        pass

    backs = get_backends()
    backend_path = (
        f"{backs[0].__module__}.{backs[0].__class__.__name__}"
        if backs else "django.contrib.auth.backends.ModelBackend"
    )
    # attach backend attribute so auth_login can proceed
    setattr(user, "backend", backend_path)
    auth_login(request, user)


def verify_member(request, member):
    """
    Guest verification page (opened from the SMS).
    On successful PIN entry:
      - ensure/create TicketLink for this ticket
      - flip pending -> open (single row)
      - log the user in
      - redirect to profile
    """
    token = request.GET.get("t", "") or request.POST.get("t", "")
    if not token:
        return HttpResponseBadRequest("Missing token.")

    # Unsign & validate token (30 min)
    try:
        data = signing.TimestampSigner().unsign_object(token, max_age=60 * 30)
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid or expired link.")

    if str(data.get("m")) != str(member):
        return HttpResponseBadRequest("Token mismatch.")

    loc_id    = str(data.get("loc") or "")
    ticket_id = str(data.get("ticket") or "")
    if not (loc_id and ticket_id):
        return HttpResponseBadRequest("Malformed token.")

    # Load member
    try:
        m = Member.objects.select_related("customer", "customer__user").get(number=member)
    except Member.DoesNotExist:
        return HttpResponseBadRequest("Member not found.")

    # GET -> render PIN page
    if request.method == "GET":
        return render(request, "core/verify_member.html", {"member": member, "token": token})

    # POST -> check PIN
    pin = (request.POST.get("pin") or "").strip()
    if not _check_member_pin(m, pin):
        return render(
            request,
            "core/verify_member.html",
            {"member": member, "token": token, "error": "Incorrect PIN. Try again."},
        )

    # Make sure the restaurant exists for this location
    rp = RestaurantProfile.objects.filter(omnivore_location_id=loc_id).first()
    if not rp:
        return HttpResponseBadRequest("Restaurant not wired to POS.")

    # Confirm ticket is still open and pull quick fields
    try:
        t = get_ticket(loc_id, ticket_id)
    except Exception:
        return HttpResponseBadRequest("Ticket not found or POS unavailable.")

    if not t.get("open", True):
        return HttpResponseBadRequest("This ticket is no longer open.")

    server_name = ((t.get("_embedded") or {}).get("employee") or {}).get("check_name", "") or ""
    totals      = (t or {}).get("totals") or {}
    due_cents   = int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0
    ticket_no   = t.get("ticket_number") or t.get("number") or ""

    # --- Flip PENDING -> OPEN (or create OPEN) ---------------------------------
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
        # May occur if link was sent long ago or before pending rows were created
        tl = TicketLink.objects.create(
            member=m,
            restaurant=rp,
            ticket_id=ticket_id,
            status="open",
            server_name=server_name,
            last_total_cents=due_cents,
            ticket_number=ticket_no,
            opened_at=timezone.now(),
        )
    # ---------------------------------------------------------------------------

    # Auto-login the customer
    user = getattr(getattr(m, "customer", None), "user", None)
    if user:
        _safe_login(request, user)

    # Off you go
    return redirect(reverse("core:profile"))
