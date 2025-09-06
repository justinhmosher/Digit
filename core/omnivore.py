# core/omnivore.py
import requests
from decouple import config

BASE = "https://api.omnivore.io/1.0"
API_KEY = config("OMNIVORE_API_KEY")
HEADERS = {"Api-Key": API_KEY}

def _embedded(obj, key):
    return ((obj or {}).get("_embedded") or {}).get(key, []) or []

def _err_blob(resp):
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text

def _raise_attempts(name, attempts):
    # attempts: list of dicts: {"payload": {...}, "status": int, "body": <json|text>}
    pretty = "\n".join(
        [
            f"Attempt {i+1}: status={a['status']}\n  payload={a['payload']}\n  body={a['body']}"
            for i, a in enumerate(attempts)
        ]
    )
    raise RuntimeError(f"{name} failed after {len(attempts)} attempts:\n{pretty}")

# ---------------- existing helpers you already had ----------------
def list_open_tickets(location_id: str):
    url = f"{BASE}/locations/{location_id}/tickets"
    params = {"where": "eq(open,true)"}
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()
    tickets = _embedded(r.json(), "tickets")
    open_tix = [t for t in tickets if t.get("open") is True]
    emp_key = "_embedded"
    for t in open_tix:
        emp = ((t.get(emp_key) or {}).get("employee") or {})
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
    return _embedded(r.json(), "items")

def get_ticket_payments(location_id: str, ticket_id: str):
    url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/payments"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return _embedded(r.json(), "payments")

def list_tender_types(location_id: str):
    """Useful for debugging / choosing the correct tender type id."""
    url = f"{BASE}/locations/{location_id}/tender_types"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return _embedded(r.json(), "tender_types")

# ---------------- resilient payment poster ----------------
# ---------------- resilient payment poster (category-aware) ----------------
def create_payment_with_tender_type(
    location_id: str,
    ticket_id: str,
    amount_cents: int,
    *,
    tender_type_id: str | None,   # ignored for this adapter
    reference: str,               # not sent to Omnivore (adapter rejects it)
    tip_cents: int | None = None,
):
    """
    This adapter requires a CASH payment and a TIP field, and rejects name/reference/tender_type.
    Post the minimal schema it accepts.
    """
    url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/payments"

    body = {
        "type": "cash",                     # <- this adapter accepts 'cash'
        "amount": int(amount_cents),
        "tip": int(tip_cents or 0),         # <- tip is REQUIRED (0 allowed)
    }

    r = requests.post(
        url,
        json=body,
        headers={**HEADERS, "Content-Type": "application/json"},
        timeout=10,
    )

    # helpful error text
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": r.text}

    if not r.ok:
        raise RuntimeError(f"Omnivore {r.status_code} {url} -> {payload}")

    return payload



# ---- Backwards-compat alias ----
from decouple import config as _cfg

def create_external_payment(
    location_id: str,
    ticket_id: str,
    amount_cents: int,
    reference: str,
    *,
    tender_type_id: str | None = None,
    name: str | None = "Dine N Dash",
    tip_cents: int | None = None,
):
    """
    Compatibility shim so old imports keep working.
    Forwards to create_payment_with_tender_type.
    """
    if tender_type_id is None:
        tender_type_id = _cfg("OMNIVORE_TENDER_TYPE_ID", default="100")
    return create_payment_with_tender_type(
        location_id=location_id,
        ticket_id=ticket_id,
        amount_cents=amount_cents,
        tender_type_id=str(tender_type_id),
        reference=reference,
        name=name,
        tip_cents=tip_cents,
    )
