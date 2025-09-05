# core/omnivore.py
import requests
from decouple import config

BASE = "https://api.omnivore.io/1.0"
API_KEY = config("OMNIVORE_API_KEY")
HEADERS = {"Api-Key": API_KEY}

def _embedded(obj, key):
    """Safely extract a list from HAL-style _embedded responses."""
    return ((obj or {}).get("_embedded") or {}).get(key, []) or []

def list_open_tickets(location_id: str):
    """
    Return a *list of ticket dicts* for OPEN tickets only.
    Also includes a few convenience fields normalized for matching.
    """
    url = f"{BASE}/locations/{location_id}/tickets"
    # Ask Omnivore to pre-filter to open tickets (still safe to double-check)
    params = {"where": "eq(open,true)"}
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()

    tickets = _embedded(r.json(), "tickets")
    open_tix = [t for t in tickets if t.get("open") is True]

    # Optional: add a “match_text” you can search (id, ticket_number, server check_name)
    for t in open_tix:
        emp = ((t.get("_embedded") or {}).get("employee") or {})
        t["match_text"] = " ".join(
            str(x or "")
            for x in [
                t.get("id"),
                t.get("ticket_number"),
                emp.get("check_name"),
                emp.get("first_name"),
                emp.get("last_name"),
            ]
        ).lower()
    return open_tix

def get_ticket(location_id: str, ticket_id: str):
    url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def get_ticket_items(location_id: str, ticket_id: str):
    url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/items"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    # Items are under _embedded.items
    return _embedded(r.json(), "items")

def get_ticket_payments(location_id: str, ticket_id: str):
    url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/payments"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return _embedded(r.json(), "payments")

def create_external_payment(location_id: str, ticket_id: str, amount_cents: int, reference: str):
    """
    Records an external tender (e.g., you charged via Stripe) so the POS ticket closes.
    """
    url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/payments"
    body = {
        "amount": amount_cents,        # integer cents
        "type": "external",
        "name": "Dine N Dash",
        "reference": reference,
    }
    r = requests.post(url, json=body, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
    r.raise_for_status()
    return r.json()
