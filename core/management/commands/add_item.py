# core/management/commands/add_item.py
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction
import json

from core.models import TicketLink, RestaurantProfile


class Command(BaseCommand):
    help = (
        "FAKE MODE: Append a fixed set of items to a local ticket snapshot "
        "(no external API calls). Accepts ticket ID or ticket NUMBER."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "ticket",
            help="Ticket ID (e.g., tkt_1011_55602) OR ticket number (e.g., 1011)",
        )
        parser.add_argument(
            "--restaurant-id",
            type=int,
            help="Optional: restrict search to a specific RestaurantProfile id",
        )
        parser.add_argument(
            "--debug",
            action="store_true",
            help="Print details of what was appended and new totals.",
        )

    def handle(self, *args, **opts):
        ticket_key = str(opts["ticket"]).strip()
        rest_id = opts.get("restaurant_id")
        debug = bool(opts.get("debug"))

        # ---------- Resolve restaurant (optional) ----------
        rp = None
        if rest_id:
            rp = RestaurantProfile.objects.filter(id=rest_id).first()
            if not rp:
                raise CommandError(f"Restaurant id={rest_id} not found.")

        # ---------- Build base queryset for TicketLink ----------
        tl_qs = TicketLink.objects.all()
        if rp:
            tl_qs = tl_qs.filter(restaurant=rp)

        # Try by ticket_id first
        tl = (
            tl_qs.filter(ticket_id=ticket_key)
            .order_by("-opened_at")
            .first()
        )
        # Fallback: ticket_number
        if not tl:
            tl = (
                tl_qs.filter(ticket_number=str(ticket_key))
                .order_by("-opened_at")
                .first()
            )

        if not tl:
            raise CommandError(
                f"Ticket not found by id or number: {ticket_key}"
                + (f" (restaurant_id={rest_id})" if rest_id else "")
            )

        if tl.status != "open":
            self.stderr.write(
                self.style.WARNING(
                    f"Ticket {tl.ticket_id} is not open (status={tl.status}); "
                    "adding items anyway and updating snapshot."
                )
            )

        # ---------- Fixed items to append ----------
        # These are your demo items. All prices are in cents.
        fixed = [
            {"name": "Pizza",            "menu_item_id": "101", "quantity": 1, "unit_cents": 1899},
            {"name": "Garlic Bread",     "menu_item_id": "200", "quantity": 1, "unit_cents":  599},
            {"name": "Taco Appetizers",  "menu_item_id": "201", "quantity": 2, "unit_cents":  995},
            {"name": "Calamari",         "menu_item_id": "206", "quantity": 1, "unit_cents": 1295},
            {"name": "Bruschetta",       "menu_item_id": "207", "quantity": 1, "unit_cents":  995},
        ]

        items = list(tl.items_json or [])
        for it in fixed:
            qty = int(it.get("quantity", 1))
            unit = int(it.get("unit_cents", 0))
            line = unit * qty
            items.append({
                "name": it.get("name") or "Item",
                "menu_item_id": it.get("menu_item_id") or "",
                "quantity": qty,
                "unit_cents": unit,
                "line_total_cents": line,
            })

        # ---------- Recompute snapshot totals ----------
        subtotal = sum(int(r.get("line_total_cents") or 0) for r in items)

        # keep existing tax/tip if already set; otherwise compute a simple demo tax
        tax = int(tl.tax_cents or round(subtotal * 0.0825))  # ~8.25% demo tax
        tip = int(tl.tip_cents or 0)

        with transaction.atomic():
            tl.items_json = items
            tl.total_cents = subtotal           # pre-tax subtotal
            tl.tax_cents = tax
            tl.last_total_cents = subtotal      # used in some views as fallback
            tl.save(update_fields=["items_json", "total_cents", "tax_cents", "last_total_cents"])

        if debug:
            self.stdout.write(self.style.NOTICE("Appended items:"))
            self.stdout.write(json.dumps(fixed, indent=2))

        self.stdout.write(self.style.SUCCESS("Order items added successfully (FAKE MODE)."))

        out = {
            "ticket_id": tl.ticket_id,
            "ticket_number": tl.ticket_number,
            "open": (tl.status == "open"),
            "subtotal_cents": tl.total_cents or 0,
            "tax_cents": tl.tax_cents or 0,
            "total_cents": (tl.total_cents or 0) + (tl.tax_cents or 0),
            "items_count": len(tl.items_json or []),
        }
        self.stdout.write("\n[Final Ticket Totals]")
        self.stdout.write(json.dumps(out, indent=2))


