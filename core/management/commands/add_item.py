# core/management/commands/add_item.py
from django.core.management.base import BaseCommand, CommandError
import json, time, requests
from core.omnivore import BASE, HEADERS  # BASE="https://api.omnivore.io/1.0", HEADERS has Api-Key

class Command(BaseCommand):
    help = "Add a fixed order to an existing POS ticket (location always cgX7jbLi). Prompts only for ticket."

    def add_arguments(self, parser):
        parser.add_argument("ticket", help="Existing, open Omnivore ticket id")

    def handle(self, *args, **opts):
        location_id = "cgX7jbLi"
        ticket_id   = opts["ticket"]

        url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/items"
        headers = dict(HEADERS)

        # Fixed order (no open-priced item)
        payload = {
            "items": [
                # Pizza (101) with Special price level (2)
                {"menu_item": "101", "quantity": 1, "price_level": "2", "seat": 1},
                # Appetizers without required options
                {"menu_item": "200", "quantity": 1, "seat": 1},
                {"menu_item": "201", "quantity": 2, "seat": 1},
                {"menu_item": "206", "quantity": 1, "seat": 2},
                {"menu_item": "207", "quantity": 1, "seat": 2},
            ]
        }

        self.stdout.write(self.style.NOTICE(f"POST {url}"))
        self.stdout.write(self.style.NOTICE(json.dumps(payload, indent=2)))

        resp = self._post_with_retries(url, headers, payload)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code not in (200, 201, 202):
            raise CommandError(f"Omnivore error {resp.status_code}: {json.dumps(data, indent=2)}")

        self.stdout.write(self.style.SUCCESS("Order items added successfully!"))
        self.stdout.write(json.dumps(data, indent=2))

    def _post_with_retries(self, url, headers, payload, retries=2, timeout=12):
        attempt = 0
        while attempt <= retries:
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = 1.5 * (attempt + 1)
                    self.stderr.write(self.style.WARNING(f"HTTP {resp.status_code}, retrying in {wait:.1f}s"))
                    time.sleep(wait)
                    attempt += 1
                    continue
                return resp
            except requests.RequestException as e:
                wait = 1.5 * (attempt + 1)
                self.stderr.write(self.style.WARNING(f"Request error: {e}, retrying in {wait:.1f}s"))
                time.sleep(wait)
                attempt += 1
        raise CommandError("Failed to POST to Omnivore after retries.")
