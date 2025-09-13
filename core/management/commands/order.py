# core/management/commands/order.py
from django.core.management.base import BaseCommand, CommandError
from decouple import config
import json
import requests

# Reuse your Omnivore config/constants
from core.omnivore import BASE, HEADERS

class Command(BaseCommand):
    help = "Open a POS ticket via Omnivore (for testing/linking). No items are added."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)

        p_create = sub.add_parser("create", help="Create a new ticket (no items).")
        p_create.add_argument("--emp", "--employee", dest="employee", default="100",
                              help="Employee ID (string/int id exposed by Omnivore). Default: 100")
        p_create.add_argument("--rc", "--revenue-center", dest="revenue_center", default="1",
                              help="Revenue center ID. Default: 1")
        p_create.add_argument("--order-type", dest="order_type", default="2",
                              help="Order type ID. Default: 2")
        p_create.add_argument("--auto-send", dest="auto_send", action="store_true", default=True,
                              help="Auto-send to kitchen (default True).")
        p_create.add_argument("--no-auto-send", dest="auto_send", action="store_false",
                              help="Disable auto-send.")
        p_create.add_argument("--debug", action="store_true", help="Print request/response bodies.")

    def handle(self, *args, **opts):
        action = opts["action"]
        if action == "create":
            return self._create(opts)
        raise CommandError(f"Unknown action: {action}")

    def _create(self, opts):
        location_id = config("OMNIVORE_LOCATION_ID", default="").strip()
        if not location_id:
            raise CommandError("OMNIVORE_LOCATION_ID is not set in your environment.")

        # IMPORTANT: These must be sent as strings/integers (NOT objects)
        employee       = str(opts["employee"])
        revenue_center = str(opts["revenue_center"])
        order_type     = str(opts["order_type"])
        auto_send      = bool(opts["auto_send"])
        debug          = bool(opts.get("debug"))

        url = f"{BASE}/locations/{location_id}/tickets"
        payload = {
            "employee": employee,            # string/int id (NOT an object)
            "revenue_center": revenue_center,# string/int id (NOT an object)
            "order_type": order_type,        # string/int id (NOT an object)
            "auto_send": auto_send,          # boolean
        }

        self.stdout.write(
            self.style.NOTICE(
                f"Creating ticket at {location_id} (emp={employee}, rc={revenue_center}, "
                f"order_type={order_type}, auto_send={auto_send})…"
            )
        )

        if debug:
            self.stdout.write("\nRequest:")
            self.stdout.write(f"POST {url}")
            self.stdout.write("Payload: " + json.dumps(payload, indent=2))

        r = requests.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=15)

        # Try to parse body for helpful error output
        try:
            body = r.json()
        except Exception:
            body = r.text

        if not r.ok:
            if debug:
                self.stdout.write("\nResponse:")
                self.stdout.write(f"Status : {r.status_code}")
                self.stdout.write("Body   : " + (json.dumps(body, indent=2) if isinstance(body, dict) else str(body)))
            raise CommandError(
                f"Ticket create failed: {r.status_code} {url}\n"
                f"{json.dumps(body, indent=2) if isinstance(body, dict) else body}"
            )

        ticket_id = str(body.get("id") or "")
        ticket_no = body.get("ticket_number") or body.get("number") or ticket_id

        self.stdout.write(self.style.SUCCESS(f"Created ticket: id={ticket_id} number={ticket_no}"))
        # Print raw JSON when debugging so you can see everything Omnivore returned
        if debug:
            self.stdout.write("\nResponse JSON:")
            self.stdout.write(json.dumps(body, indent=2))