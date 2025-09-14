from __future__ import annotations

import json
from datetime import timedelta, datetime

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_protect
from django.views.decorators.http import require_GET, require_POST

from .models import ManagerProfile, RestaurantProfile, StaffProfile, TicketLink
from .omnivore import get_ticket, get_ticket_items


def _require_manager(request: HttpRequest):
    """Return (manager_profile, restaurant) or (None, None)."""
    mp = getattr(request.user, "manager_profile", None)
    if mp is None:
        mp = (
            ManagerProfile.objects
            .select_related("restaurant")
            .filter(user=request.user)
            .first()
        )
    if not mp or not getattr(mp, "restaurant", None):
        return None, None
    return mp, mp.restaurant


@login_required
def manager_dashboard(request: HttpRequest) -> HttpResponse:
    mp, restaurant = _require_manager(request)
    if not mp:
        return redirect("/restaurant/signin?tab=manager")

    if restaurant and request.session.get("current_restaurant_id") != restaurant.id:
        request.session["current_restaurant_id"] = restaurant.id
        request.session.modified = True

    return render(request, "core/manager_dashboard.html", {"mp": mp, "restaurant": restaurant})


from datetime import datetime, time, timedelta
from django.utils.dateparse import parse_date
from django.db.models import Q

@ensure_csrf_cookie
@require_GET
@login_required
def manager_api_state(request: HttpRequest) -> JsonResponse:
    """
    Dashboard data for manager:
      - staff list (name only + ids for Remove)
      - open tickets summary
      - recent closed tickets within optional date range
    """
    mp, rp = _require_manager(request)
    if not mp or not rp:
        return JsonResponse({"ok": False, "error": "Not a manager for any restaurant."}, status=403)

    q = (request.GET.get("q") or "").strip().lower()
    start = (request.GET.get("start") or "").strip()
    end   = (request.GET.get("end") or "").strip()

    # Staff (derive a simple name from the user)
    staff = []
    staff_qs = (
        StaffProfile.objects
        .select_related("user")
        .filter(restaurant=rp)
        .order_by("user__email")
    )
    for s in staff_qs:
        u = getattr(s, "user", None)
        email = getattr(u, "email", "") if u else ""
        name = (u.get_full_name() if u else "") or (email.split("@")[0] if email else "")
        staff.append({"id": s.id, "name": name})

    # Open tickets, group by ticket id
    open_map = {}
    open_qs = (
        TicketLink.objects
        .select_related("member")
        .filter(restaurant=rp, status="open")
        .order_by("-opened_at")[:400]
    )
    for tl in open_qs:
        entry = open_map.setdefault(tl.ticket_id, {
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number or None,
            "server": tl.server_name or "",
            "members": [],
            "due_cents": 0,
        })
        entry["members"].append(tl.member.number if tl.member else "")
        entry["due_cents"] = max(entry["due_cents"], tl.last_total_cents or 0)
    open_list = list(open_map.values())

    # Date window for "recent"
    recent_qs = TicketLink.objects.select_related("member").filter(restaurant=rp, status="closed")
    # optional range filter
    if start:
        try:
            recent_qs = recent_qs.filter(closed_at__date__gte=start)
        except Exception:
            pass
    if end:
        try:
            recent_qs = recent_qs.filter(closed_at__date__lte=end)
        except Exception:
            pass

    recent = []
    for tl in recent_qs.order_by("-closed_at")[:500]:
        row = {
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number or None,
            "member": tl.member.number if tl.member else "",
            "server": tl.server_name or "",
            "closed_at": tl.closed_at.strftime("%Y-%m-%d %H:%M") if tl.closed_at else "",
            # show what was actually paid when available
            "total_cents": (tl.paid_cents or tl.total_cents or tl.last_total_cents or 0),
        }
        if q:
            hay = f'{row["ticket_number"] or ""} {row["member"] or ""}'.lower()
            if q not in hay:
                continue
        recent.append(row)

    return JsonResponse({"ok": True, "staff": staff, "open": open_list, "recent": recent})



@csrf_protect
@require_POST
@login_required
def manager_api_remove_staff(request: HttpRequest) -> JsonResponse:
    mp, rp = _require_manager(request)
    if not mp or not rp:
        return JsonResponse({"ok": False, "error": "Not authorized."}, status=403)

    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = {}
    sid = data.get("staff_id")
    if not sid:
        return JsonResponse({"ok": False, "error": "Missing staff_id."}, status=400)

    sp = StaffProfile.objects.filter(id=sid, restaurant=rp).first()
    if not sp:
        return JsonResponse({"ok": False, "error": "Staff not found."}, status=404)

    sp.restaurant = None  # unlink access
    sp.save(update_fields=["restaurant"])

    return JsonResponse({"ok": True})



# DROP-IN: replace the whole function in core/views_manager.py

@require_GET
@login_required
def manager_api_ticket_detail(request: HttpRequest, ticket_id: str) -> JsonResponse:
    mp, rp = _require_manager(request)
    if not mp or not rp:
        return JsonResponse({"ok": False, "error": "Not authorized."}, status=403)

    tl = (
        TicketLink.objects
        .filter(restaurant=rp, ticket_id=str(ticket_id))
        .order_by("-opened_at")
        .first()
    )
    if not tl:
        return JsonResponse({"ok": False, "error": "Ticket not found."}, status=404)

    # ---------- OPEN: pull live, but display with Subtotal = Total ----------
    if tl.status == "open" and (rp.omnivore_location_id or "").strip():
        try:
            t = get_ticket(rp.omnivore_location_id, tl.ticket_id)
            items = get_ticket_items(rp.omnivore_location_id, tl.ticket_id)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"POS error: {e}"}, status=502)

        rows = []
        for it in items:
            qty = int(it.get("quantity", 1) or 1)
            cents = int(it.get("price", 0) or 0)
            rows.append({"name": it.get("name"), "qty": qty, "cents": cents})

        totals = (t or {}).get("totals") or {}
        tax = int(totals.get("tax", 0) or 0)
        tip = int(totals.get("tip", 0) or 0)

        # Prefer "due"; otherwise fall back to POS "total", then sum(qty*price)+tax+tip
        try:
            due_val = totals.get("due")
            total = int(due_val) if due_val is not None else int(totals.get("total", 0) or 0)
        except Exception:
            total = 0
        if total <= 0:
            # last fallback: compute naive total from line items + tax + tip
            line_sum = sum(int(r["qty"]) * int(r["cents"]) for r in rows)
            total = line_sum + tax + tip

        # display rule for OPEN tickets: Subtotal == Total
        display_subtotal = total

        return JsonResponse({
            "ok": True,
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number or t.get("ticket_number") or t.get("number") or tl.ticket_id,
            "member": tl.member.number if tl.member else "",
            "server": tl.server_name or ((t.get("_embedded") or {}).get("employee") or {}).get("check_name", ""),
            "items": rows,
            "subtotal_cents": display_subtotal,   # <- Subtotal shown equals Total
            "tax_cents": tax,
            "tip_cents": tip,
            "total_cents": total,
            "is_open": True,
        })

    # ---------- CLOSED: use your snapshot mapping ----------
    rows = []
    for it in (tl.items_json or []):
        name  = it.get("name") or it.get("label") or "Item"
        qty   = int(it.get("qty") or it.get("quantity") or 1)
        cents = int(it.get("cents") or it.get("price") or 0)
        rows.append({"name": name, "qty": qty, "cents": cents})

    return JsonResponse({
        "ok": True,
        "ticket_id": tl.ticket_id,
        "ticket_number": tl.ticket_number or tl.ticket_id,
        "member": tl.member.number if tl.member else "",
        "server": tl.server_name or "",
        # CLOSED mapping: Subtotal = total_cents, Total = paid_cents
        "items": rows,
        "subtotal_cents": int(tl.total_cents or 0),
        "tax_cents": int(tl.tax_cents or 0),
        "tip_cents": int(tl.tip_cents or 0),
        "total_cents": int(tl.paid_cents or (tl.total_cents or 0) + (tl.tax_cents or 0) + (tl.tip_cents or 0)),
        "is_open": False,
    })

from io import BytesIO
from datetime import datetime, time, timedelta
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from django.db.models import Q
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

@require_GET
@login_required
def manager_export(request: HttpRequest) -> HttpResponse:
    mp, rp = _require_manager(request)
    if not mp or not rp:
        return HttpResponse("Not authorized.", status=403)

    q = (request.GET.get("q") or "").strip().lower()
    start = (request.GET.get("start") or "").strip()
    end   = (request.GET.get("end") or "").strip()

    qs = (
        TicketLink.objects
        .select_related("member")
        .filter(restaurant=rp, status="closed")
    )
    if start:
        try:
            qs = qs.filter(closed_at__date__gte=start)
        except Exception:
            pass
    if end:
        try:
            qs = qs.filter(closed_at__date__lte=end)
        except Exception:
            pass

    rows = []
    for tl in qs.order_by("closed_at"):
        # optional search filter
        if q:
            hay = f"{(tl.ticket_number or tl.ticket_id)} {(tl.member.number if tl.member else '')}".lower()
            if q not in hay:
                continue

        subtotal = int(tl.total_cents or 0)   # <-- per your mapping
        tax      = int(tl.tax_cents or 0)
        tip      = int(tl.tip_cents or 0)
        total    = int(tl.paid_cents or (subtotal + tax + tip))  # <-- Total shows what was paid

        rows.append({
            "closed": tl.closed_at,
            "ticket": tl.ticket_number or tl.ticket_id,
            "member": tl.member.number if tl.member else "",
            "server": tl.server_name or "",
            "subtotal": subtotal,
            "tax": tax,
            "tip": tip,
            "total": total,
            "pos_ref": tl.pos_ref or "",
        })

    # Build workbook
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Closed"

    headers = ["Closed", "Ticket", "Member", "Server", "Subtotal", "Tax", "Tip", "Total", "POS Ref"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    currency_fmt = u'[$$-409]#,##0.00'

    for r in rows:
        ws.append([
            r["closed"].strftime("%Y-%m-%d %H:%M") if r["closed"] else "",
            r["ticket"],
            r["member"],
            r["server"],
            r["subtotal"]/100.0,
            r["tax"]/100.0,
            r["tip"]/100.0,
            r["total"]/100.0,
            r["pos_ref"],
        ])

    # Format currency columns E..H
    if rows:
        for row in ws.iter_rows(min_row=2, min_col=5, max_col=8):
            for cell in row:
                cell.number_format = currency_fmt

    # Column widths
    widths = [18, 12, 14, 12, 12, 12, 12, 12, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Grand total row (sum of Total column)
    total_row = len(rows) + 2
    ws.cell(row=total_row, column=7, value="Grand total").font = Font(bold=True)
    total_col = get_column_letter(8)
    ws.cell(row=total_row, column=8, value=f"=SUM({total_col}2:{total_col}{total_row-1})").number_format = currency_fmt
    ws.cell(row=total_row, column=8).font = Font(bold=True)

    # Return file
    rest_name = getattr(rp, "dba_name", "") or getattr(rp, "legal_name", "") or "restaurant"
    filename = f"{rest_name.replace(' ', '_')}_closed_{timezone.now().strftime('%Y%m%d')}.xlsx"

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    resp = HttpResponse(bio.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp
