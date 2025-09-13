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


# ---- Helpers ---------------------------------------------------------------

def _check_member_pin(member_obj: Member, raw_pin: str) -> bool:
    """
    Validate a 4-digit PIN against CustomerProfile.pin_hash using Django's
    password hashers. Returns True if it matches, False otherwise.
    """
    if not member_obj:
        return False
    customer = getattr(member_obj, "customer", None)
    if not customer:
        return False
    pin_hash = getattr(customer, "pin_hash", "") or ""
    if not pin_hash:
        return False
    # Check hashed PIN (supports PBKDF2/BCrypt/etc.)
    return check_password(str(raw_pin), pin_hash)


def _due_from_ticket(t: dict) -> int:
    totals = (t or {}).get("totals") or {}
    if totals.get("due") is not None:
        return int(totals.get("due") or 0)
    return int(totals.get("total") or 0)


# ---- View ------------------------------------------------------------------

def verify_member(request, member):
    token = request.GET.get("t", "")
    if not token:
        return HttpResponseBadRequest("Missing token.")

    try:
        data = signing.TimestampSigner().unsign_object(token, max_age=60 * 30)
    except signing.BadSignature:
        return HttpResponseBadRequest("Invalid or expired link.")

    if str(data.get("m")) != str(member):
        return HttpResponseBadRequest("Token mismatch.")

    loc_id = str(data.get("loc") or "")
    ticket_id = str(data.get("ticket") or "")
    if not loc_id or not ticket_id:
        return HttpResponseBadRequest("Missing location or ticket.")

    # Member + customer
    try:
        m = Member.objects.select_related("customer__user").get(number=member)
    except Member.DoesNotExist:
        return HttpResponseBadRequest("Member not found.")

    customer = m.customer
    if not customer or not customer.pin_hash:
        # You can soften this message if desired
        return render(
            request,
            "core/verify_member.html",
            {"member": member, "token": token, "error": "No PIN on file. Please set your PIN first."},
        )

    # Restaurant by Omnivore location id
    rp = RestaurantProfile.objects.filter(omnivore_location_id=loc_id).first()
    if not rp:
        return HttpResponseBadRequest("Restaurant not found for this location.")

    if request.method == "POST":
        pin = (request.POST.get("pin") or "").strip()

        if not check_password(pin, customer.pin_hash):
            return render(
                request,
                "core/verify_member.html",
                {"member": member, "token": token, "error": "Incorrect PIN. Try again."},
            )

        # Confirm ticket still open in POS
        try:
            t = get_ticket(loc_id, ticket_id)
        except Exception:
            return HttpResponseBadRequest("Ticket not found or POS unavailable.")

        if not t.get("open", True):
            return HttpResponseBadRequest("This ticket is no longer open.")

        # Pull a few fields from the POS ticket
        emp = ((t.get("_embedded") or {}).get("employee") or {})
        server_name = emp.get("check_name") or (" ".join([emp.get("first_name",""), emp.get("last_name","")]).strip())
        totals = (t.get("totals") or {})
        due_cents = int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0

        # If a pending link exists for this (member, restaurant, ticket), flip it to open
        tl = (
            TicketLink.objects
            .filter(member=m, restaurant=rp, ticket_id=ticket_id, status="pending")
            .first()
        )
        if tl:
            tl.status = "open"
            tl.server_name = server_name or ""
            tl.last_total_cents = due_cents
            tl.ticket_number = str(t.get("ticket_number") or "")
            tl.table = t.get("table") or ""
            tl.raw_ticket_json = t
            tl.save()
        else:
            # Create/open link
            tl, _ = TicketLink.objects.get_or_create(
                member=m,
                restaurant=rp,
                ticket_id=ticket_id,
                status="open",
                defaults={
                    "server_name": server_name or "",
                    "last_total_cents": due_cents,
                    "ticket_number": str(t.get("ticket_number") or ""),
                    "table": t.get("table") or "",
                    "raw_ticket_json": t,
                },
            )

        # Log the customer in, then send to their profile
        user = customer.user
        # Ensure a backend is set for manual login (common in OTP flows)
        if not hasattr(user, "backend"):
            from django.contrib.auth import get_backends
            backend = next(iter(get_backends()))
            user.backend = f"{backend.__module__}.{backend.__class__.__name__}"
        auth_login(request, user)

        return redirect(reverse("core:profile"))

    # GET -> show PIN form
    return render(request, "core/verify_member.html", {"member": member, "token": token})
