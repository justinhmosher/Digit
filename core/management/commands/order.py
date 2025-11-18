# core/management/commands/order.py
from django.core.management.base import BaseCommand, CommandError
from decouple import config
import json
from core import omnivore  # exposes IS_FAKE + unified functions
from core.omnivore import BASE, HEADERS  # safe in both modes

class Command(BaseCommand):
    help = "Create a POS ticket and (by default) attach a fixed order — works in FAKE or REAL modes."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

        p_create = sub.add_parser("create", help="Create a new ticket (and add items unless --no-items).")
        p_create.add_argument("--emp", "--employee", dest="employee", default="100",
                              help="Employee ID (string/int). Default: 100")
        p_create.add_argument("--rc", "--revenue-center", dest="revenue_center", default="1",
                              help="Revenue center ID. Default: 1")
        p_create.add_argument("--order-type", dest="order_type", default="2",
                              help="Order type ID. Default: 2")
        p_create.add_argument("--auto-send", dest="auto_send", action="store_true", default=True,
                              help="Auto-send to kitchen (default True).")
        p_create.add_argument("--no-auto-send", dest="auto_send", action="store_false",
                              help="Disable auto-send.")
        p_create.add_argument("--no-items", dest="with_items", action="store_false", default=True,
                              help="Create the ticket but do not add the fixed items.")
        p_create.add_argument("--debug", action="store_true", help="Print request/response bodies.")

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "create":
            return self._create_with_optional_items(opts)
        raise CommandError(f"Unknown action: {action}")

    def _create_with_optional_items(self, opts):
        location_id = config("OMNIVORE_LOCATION_ID", default="").strip()
        if not location_id:
            raise CommandError("OMNIVORE_LOCATION_ID is not set in your environment.")

        employee       = str(opts["employee"])
        revenue_center = str(opts["revenue_center"])
        order_type     = str(opts["order_type"])
        auto_send      = bool(opts["auto_send"])
        with_items     = bool(opts["with_items"])
        debug          = bool(opts.get("debug"))

        self.stdout.write(self.style.NOTICE(
            f"Creating ticket at {location_id} (emp={employee}, rc={revenue_center}, "
            f"order_type={order_type}, auto_send={auto_send})…"
        ))

        # Create ticket (fake or real routed by omnivore.create_ticket)
        try:
            if debug:
                self.stdout.write("\n[Ticket Create] Params:")
                self.stdout.write(json.dumps({
                    "location_id": location_id,
                    "employee": employee,
                    "revenue_center": revenue_center,
                    "order_type": order_type,
                    "auto_send": auto_send
                }, indent=2))

            body = omnivore.create_ticket(
                location_id,
                employee=employee,
                revenue_center=revenue_center,
                order_type=order_type,
                auto_send=auto_send,
            )
        except Exception as e:
            raise CommandError(f"Ticket create failed: {e}")

        ticket_id = str(body.get("id") or "")
        ticket_no = body.get("ticket_number") or body.get("number") or ticket_id
        self.stdout.write(self.style.SUCCESS(f"Created ticket: id={ticket_id} number={ticket_no}"))

        # Attach a fixed order unless disabled
        if with_items:
            fixed_payload = {
                "items": [
                    {"menu_item": "101", "quantity": 1, "price_level": "2", "seat": 1},  # Pizza (special price)
                    {"menu_item": "200", "quantity": 1, "seat": 1},                      # Mozz Sticks
                    {"menu_item": "201", "quantity": 2, "seat": 1},                      # Garlic Bread x2
                    {"menu_item": "206", "quantity": 1, "seat": 2},                      # Wings (6)
                    {"menu_item": "207", "quantity": 1, "seat": 2},                      # Bruschetta
                ]
            }

            if debug:
                self.stdout.write("\n[Add Items] Payload:")
                self.stdout.write(json.dumps(fixed_payload, indent=2))

            try:
                add_resp = omnivore.add_items(location_id, ticket_id, items=fixed_payload["items"])
            except Exception as e:
                raise CommandError(f"Add items failed: {e}")

            self.stdout.write(self.style.SUCCESS("Order items added successfully!"))
            if debug:
                self.stdout.write(json.dumps(add_resp, indent=2))

            # Show final totals snapshot
            try:
                final = omnivore.get_ticket(location_id, ticket_id)
                snapshot = {
                    "ticket_id": final.get("id"),
                    "ticket_number": final.get("ticket_number"),
                    "open": final.get("open"),
                    "subtotal_cents": final.get("subtotal"),
                    "tax_cents": final.get("tax"),
                    "total_cents": final.get("total"),
                    "items_count": len(final.get("items", [])),
                }
            except Exception:
                snapshot = None

            if snapshot:
                self.stdout.write(self.style.NOTICE("\n[Final Ticket Totals]"))
                self.stdout.write(json.dumps(snapshot, indent=2))

        if debug:
            self.stdout.write("\n[Create Response JSON]:")
            self.stdout.write(json.dumps(body, indent=2))
