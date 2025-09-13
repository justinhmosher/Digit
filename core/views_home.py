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

# Stripe for card brand/last4 on Profile
import stripe

from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render, redirect, resolve_url
from django.views.decorators.csrf import ensure_csrf_cookie

from .models import CustomerProfile, Member, TicketLink

# add these if not already present in this file
import stripe
from decouple import config
stripe.api_key = config("STRIPE_SK")


def _member_for_user(user) -> Member | None:
    if not getattr(user, "is_authenticated", False):
        return None
    cp = getattr(user, "customer_profile", None)
    if not cp:
        return None
    return Member.objects.filter(customer=cp).first()

def _user_has_customer(user) -> bool:
    return bool(getattr(user, "customer_profile", None))

def _due_member_for_user(user) -> Member | None:
    """
    Return the most recently-created Member for ANY CustomerProfile owned by this user.
    This avoids issues if multiple CustomerProfiles were created during testing.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        Member.objects
        .filter(customer__user=user)
        .order_by("-id")
        .first()
    )


@ensure_csrf_cookie
def customer_home(request: HttpRequest) -> HttpResponse:
    """
    Render the profile/home page with:
      - has_customer: bool
      - has_live_order: bool
      - member_number: str
      - card_brand / card_last4 (from Stripe, if available)
    """
    user = request.user
    has_customer = False
    has_live_order = False
    member_number = ""
    card_brand = ""
    card_last4 = ""

    if user.is_authenticated:
        # Do they have any customer profile?
        cp = CustomerProfile.objects.filter(user=user).order_by("-id").first()
        has_customer = cp is not None

        # Prefer the member tied to the newest *open* link
        open_link = (
            TicketLink.objects
            .filter(member__customer__user=user, status="open")
            .select_related("member")
            .order_by("-opened_at")
            .first()
        )
        if open_link:
            has_live_order = True
            member_number = (open_link.member.number or "").strip()
        else:
            # Fallback: latest member for this user (for the Profile tab)
            m = (
                Member.objects
                .filter(customer__user=user)
                .order_by("-id")
                .first()
            )
            if m:
                member_number = (m.number or "").strip()

        # ---- Stripe card details (brand / last4) ----
        if cp and cp.stripe_customer_id:
            pm_id = (cp.default_payment_method or "").strip()

            try:
                # (1) If no stored PM id, try the Customer's invoice_settings.default_payment_method
                if not pm_id:
                    cust = stripe.Customer.retrieve(cp.stripe_customer_id)
                    pm_id = (cust.get("invoice_settings", {}) or {}).get("default_payment_method") or ""

                # (2) Still nothing? Use the first attached card on the customer
                if not pm_id:
                    pms = stripe.PaymentMethod.list(
                        customer=cp.stripe_customer_id,
                        type="card",
                        limit=1,
                    )
                    if pms and pms.data:
                        pm_id = pms.data[0].id

                # (3) If we have a PM id, read brand/last4
                if pm_id:
                    pm = stripe.PaymentMethod.retrieve(pm_id)
                    card = (pm or {}).get("card") or {}
                    if card:
                        brand = (card.get("brand") or "").strip()
                        card_brand = brand[:1].upper() + brand[1:] if brand else ""
                        card_last4 = (card.get("last4") or "").strip()
            except Exception:
                # Swallow errors so the page still renders; you can log this if you like.
                pass

    ctx = {
        "has_customer": has_customer,
        "has_live_order": has_live_order,
        "member_number": member_number,
        "card_brand": card_brand,
        "card_last4": card_last4,
    }
    return render(request, "core/profile.html", ctx)



def _due_cents(ticket: dict) -> int:
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


def signout(request: HttpRequest) -> HttpResponse:
    logout(request)
    nxt = request.GET.get("next")
    if nxt and nxt.startswith("/"):
        return redirect(nxt)
    return redirect(resolve_url("core:profile"))


@require_GET
def api_ticket_receipt(request: HttpRequest, member: str) -> JsonResponse:
    """
    Return the live receipt for the most recent OPEN TicketLink for this user+member.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Auth required."}, status=401)

    # Authorize: the member number in the URL must belong to THIS user
    m = (
        Member.objects
        .filter(number=str(member), customer__user=request.user)
        .first()
    )
    if not m:
        return JsonResponse({"ok": False, "error": "Not authorized for this member."}, status=403)

    tl = (
        TicketLink.objects
        .filter(member=m, status="open")
        .select_related("restaurant")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    # POS location from the restaurant record
    loc_id = (tl.restaurant.omnivore_location_id or "").strip()
    if not loc_id:
        return JsonResponse({"ok": False, "error": "Restaurant not wired to POS."}, status=500)

    # Live POS data
    t = get_ticket(loc_id, tl.ticket_id)
    items = get_ticket_items(loc_id, tl.ticket_id)

    # Build rows + compute subtotal if POS doesn't provide it
    subtotal_calc = 0
    rows = []
    for i in items:
        qty = int(i.get("quantity", 1) or 1)
        cents = int(i.get("price", 0) or 0)
        subtotal_calc += qty * cents
        rows.append({"name": i.get("name"), "qty": qty, "cents": cents})

    totals = (t or {}).get("totals") or {}
    subtotal = int(totals.get("sub_total", subtotal_calc) or subtotal_calc)
    tax      = int(totals.get("tax", 0) or 0)
    total    = int(totals.get("total", subtotal + tax) or (subtotal + tax))
    due      = int(totals.get("due", total) or total)

    # Remember latest due for the staff board
    TicketLink.objects.filter(pk=tl.pk).update(last_total_cents=due)

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

@require_GET
def api_ticket_receipt(request: HttpRequest, member_number: str) -> JsonResponse:
    # auth guard
    def _assert_member_ownership(req: HttpRequest, num: str) -> Member | None:
        if not req.user.is_authenticated:
            return None
        m = _member_for_user(req.user)
        if not m or m.number != str(num):
            return None
        return m

    m = _assert_member_ownership(request, member_number)
    if not m:
        return JsonResponse({"ok": False, "error": "Not authorized for this member."}, status=403)

    tl = (
        TicketLink.objects
        .filter(member=m, status="open")
        .select_related("restaurant")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    location_id = (tl.restaurant.omnivore_location_id or "").strip()
    if not location_id:
        return JsonResponse({"ok": False, "error": "Restaurant not wired to POS."}, status=500)

    # Pull live ticket + items from Omnivore
    t = get_ticket(location_id, tl.ticket_id)
    items = get_ticket_items(location_id, tl.ticket_id)

    # Build line rows + compute fallback subtotal
    rows = []
    subtotal_calc = 0
    for i in items:
        qty = int(i.get("quantity", 1) or 1)
        cents = int(i.get("price", 0) or 0)
        subtotal_calc += qty * cents
        rows.append({"name": i.get("name"), "qty": qty, "cents": cents})

    totals   = (t or {}).get("totals") or {}
    subtotal = int(totals.get("sub_total", subtotal_calc) or subtotal_calc)
    tax      = int(totals.get("tax", 0) or 0)
    total    = int(totals.get("total", subtotal + tax) or (subtotal + tax))
    due      = int(totals.get("due", total) or total)

    # remember what we last saw
    tl.last_total_cents = due
    tl.save(update_fields=["last_total_cents"])

    return JsonResponse({
        "ok": True,
        "ticket_id": tl.ticket_id,
        "server": tl.server_name or "",
        "items": rows,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": total,
        "due_cents": due,
    })



@ensure_csrf_cookie
@csrf_protect
@require_POST
def api_close_tab(request: HttpRequest, member: str) -> JsonResponse:
    """
    Close the current open TicketLink for this user+member by posting a POS payment.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Auth required."}, status=401)

    m = (
        Member.objects
        .filter(number=str(member), customer__user=request.user)
        .first()
    )
    if not m:
        return JsonResponse({"ok": False, "error": "Not authorized for this member."}, status=403)

    tl = (
        TicketLink.objects
        .filter(member=m, status="open")
        .select_related("restaurant")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "No active ticket."}, status=404)

    loc_id = (tl.restaurant.omnivore_location_id or "").strip()
    if not loc_id:
        return JsonResponse({"ok": False, "error": "Restaurant not wired to POS."}, status=500)

    # Get fresh amount due from POS
    t = get_ticket(loc_id, tl.ticket_id)
    totals = (t or {}).get("totals") or {}
    amount = int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "Nothing due."}, status=400)

    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}
    reference = (data.get("reference") or f"dnd-{timezone.now().timestamp():.0f}").strip()

    # Post payment (your adapter handles tender/tip mapping)
    create_external_payment(loc_id, tl.ticket_id, amount, reference)

    # Mark link closed & store reference
    tl.status = "closed"
    tl.pos_ref = reference
    tl.closed_at = timezone.now()
    tl.save(update_fields=["status", "pos_ref", "closed_at"])

    return JsonResponse({"ok": True})