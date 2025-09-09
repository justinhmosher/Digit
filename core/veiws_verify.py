# core/views_verify.py
from django.shortcuts import render, redirect
from django.http import HttpResponseBadRequest
from django.core import signing
from django.urls import reverse
from .models import Member

# Replace this with your real pin-check once you wire it up.
def _check_member_pin(member_obj: Member, raw_pin: str) -> bool:
    # Example: assume CustomerProfile has a "pin" field
    customer = getattr(member_obj, "customer", None)
    pin_on_file = getattr(customer, "pin", None)
    return bool(pin_on_file) and str(raw_pin) == str(pin_on_file)

# core/views_verify.py (only the POST section changes)
from django.http import JsonResponse
from django.utils import timezone
from .models import TicketLink
from .omnivore import get_ticket

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

    loc_id = data.get("loc")
    ticket_id = str(data.get("ticket", ""))

    try:
        m = Member.objects.select_related("customer").get(number=member)
    except Member.DoesNotExist:
        return HttpResponseBadRequest("Member not found.")

    if request.method == "POST":
        pin = (request.POST.get("pin") or "").strip()
        if not _check_member_pin(m, pin):
            return render(
                request,
                "core/verify_member.html",
                {"member": member, "token": token, "error": "Incorrect PIN. Try again."},
            )

        # Safety: confirm the ticket is still open
        try:
            t = get_ticket(loc_id, ticket_id)
        except Exception:
            return HttpResponseBadRequest("Ticket not found or POS unavailable.")

        if not t.get("open", True):
            return HttpResponseBadRequest("This ticket is no longer open.")

        # Idempotent create: only if not already linked and open
        tl, created = TicketLink.objects.get_or_create(
            member=m,
            location_id=loc_id,
            ticket_id=ticket_id,
            defaults={
                "server_name": ((t.get("_embedded") or {}).get("employee") or {}).get("check_name", "") or "",
                "last_total": int(((t or {}).get("totals") or {}).get("due") or ((t or {}).get("totals") or {}).get("total") or 0),
                "status": "open",
            },
        )
        # If an old link exists but was closed, you can re-open or create a new one; adjust if needed.

        # Success: go wherever makes sense
        # return JsonResponse({"ok": True, "linked": True, "ticket_id": ticket_id})
        return redirect(reverse("core:profile"))

    return render(request, "core/verify_member.html", {"member": member, "token": token})
