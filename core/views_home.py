# core/views_home.py
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
    create_payment_with_tender_type,
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
from core.views_processing import (
    charge_customer_off_session,
    refund_payment_intent,
    PaymentError,
    build_idem_key,
)


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
    Customer close (Stripe Connect version):
      - Auth: <member> must belong to signed-in user
      - Body: {"tip_cents": <int>, "reference": "customer-close"}
      - Compute amount due from POS; gross = amount + tip
      - Charge saved card (customer.default_payment_method) off-session
        * destination = restaurant.stripe_account_id (if present)
      - Post to POS; if POS post fails, refund Stripe
      - Close all open TicketLinks for that ticket with snapshots
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "auth_required"}, status=401)

    # 0) Ensure the member belongs to this user
    m = (
        Member.objects
        .select_related("customer")
        .filter(number=str(member), customer__user=request.user)
        .first()
    )
    if not m:
        return JsonResponse({"ok": False, "error": "not_authorized_for_member"}, status=403)

    # Must have at least one open link to find the ticket + restaurant
    tl = (
        TicketLink.objects
        .select_related("restaurant")
        .filter(member=m, status="open")
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "no_active_ticket"}, status=404)

    rp: RestaurantProfile = tl.restaurant
    loc_id = (rp.omnivore_location_id or LOCATION_ID or "").strip()
    if not loc_id:
        return JsonResponse({"ok": False, "error": "restaurant_missing_location_id"}, status=500)

    # Parse body
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}
    reference = (data.get("reference") or "customer-close").strip()
    try:
        tip_cents = max(0, int(data.get("tip_cents") or 0))
    except Exception:
        tip_cents = 0

    # 1) Fresh due from POS (fallback to snapshots)
    try:
        t = get_ticket(loc_id, tl.ticket_id)
        totals = (t or {}).get("totals") or {}
        base_due = int(totals.get("due") if totals.get("due") is not None else totals.get("total", 0)) or 0
    except Exception:
        base_due = 0

    if base_due <= 0:
        base_due = max(
            [int(x.last_total_cents or 0) for x in TicketLink.objects.filter(ticket_id=tl.ticket_id, status="open")] or [0]
        )
    if base_due <= 0:
        return JsonResponse({"ok": False, "error": "nothing_due"}, status=400)

    gross_cents = base_due + tip_cents

    # 2) Identify billable customer + payment method
    cp: CustomerProfile | None = getattr(m, "customer", None)
    if not cp or not cp.stripe_customer_id or not cp.default_payment_method:
        return JsonResponse({"ok": False, "error": "customer_missing_payment_method"}, status=400)

    # 3) Charge via Stripe (off-session), routing funds to the restaurant’s connected acct if present
    #    Build deterministic idempotency key so accidental double-taps don’t double-charge.
    stripe_meta = {
        "ticket_id": tl.ticket_id,
        "restaurant_id": str(rp.id),
        "member_number": m.number,
        "customer_profile_id": str(cp.id),
        "source": "customer_close",
    }
    description = f"Dine N Dash — Ticket {tl.ticket_id} ({rp.display_name()})"
    idem_key = build_idem_key("cust_close", {
        "amount": gross_cents,
        "customer": cp.stripe_customer_id,
        "pm": cp.default_payment_method,
        "restaurant": rp.stripe_account_id or "",
        "ticket": tl.ticket_id,
        "tip": tip_cents,
    })

    try:
        # charge_customer_off_session already supports Connect routing (destination)
        intent = charge_customer_off_session(
            customer_id=cp.stripe_customer_id,
            payment_method_id=cp.default_payment_method,
            amount_cents=gross_cents,
            currency="usd",
            description=description,
            idempotency_key=idem_key,
            metadata=stripe_meta,
            # Optional platform fee:
            # application_fee_amount=your_platform_fee_cents_or_None,
            destination_account_id=(rp.stripe_account_id or None),
            on_behalf_of=(rp.stripe_account_id or None),
        )
    except PaymentError as e:
        # Bubble rich info for UI/observability
        return JsonResponse({
            "ok": False,
            "error": "stripe_charge_failed",
            "detail": str(e),
            "code": e.code,
            "decline_code": e.decline_code,
            "payment_intent": e.payment_intent_id,
        }, status=402)

    # 4) Post to POS. If POS fails, refund the Stripe charge to avoid charging without closing POS.
    try:
        create_payment_with_tender_type(
            location_id=loc_id,
            ticket_id=tl.ticket_id,
            amount_cents=base_due,
            tender_type_id=None,   # adapter ignores
            reference=reference,   # kept for logs
            tip_cents=tip_cents,
        )
    except Exception as pos_err:
        # Try to refund the charge
        try:
            if getattr(intent, "id", None):
                refund_payment_intent(intent.id, reason="requested_by_customer")
        except PaymentError as refund_err:
            return JsonResponse({
                "ok": False,
                "error": "pos_post_failed_and_refund_failed",
                "pos_detail": str(pos_err),
                "refund_detail": str(refund_err),
                "payment_intent": getattr(intent, "id", None),
            }, status=502)

        return JsonResponse({
            "ok": False,
            "error": "omnivore_payment_failed_refunded",
            "detail": str(pos_err),
            "payment_intent": getattr(intent, "id", None),
        }, status=502)

    # 5) Close ALL open TicketLinks for this ticket and snapshot amounts
    now = timezone.now()
    open_links = list(TicketLink.objects.filter(ticket_id=tl.ticket_id, status="open"))
    for link in open_links:
        link.status = "closed"
        link.closed_at = now
        link.total_cents = base_due
        link.tip_cents = tip_cents
        link.paid_cents = gross_cents
        link.pos_ref = reference
        link.save(update_fields=[
            "status", "closed_at",
            "total_cents", "tip_cents", "paid_cents", "pos_ref",
        ])

    return JsonResponse({
        "ok": True,
        "closed": len(open_links),
        "paid_cents": gross_cents,
        "tip_cents": tip_cents,
        "payment_intent": getattr(intent, "id", None),
        "destination": rp.stripe_account_id or None,
    })
