# core/omnivore.py
from __future__ import annotations
import os
import time
import json
import random
from pathlib import Path
from datetime import datetime, timedelta

# =====================================================================================
# MODE TOGGLE
# If OMNIVORE_FAKE=1 OR OMNIVORE_API_KEY is missing/blank, we run a fully local fake.
# =====================================================================================
_FAKE_MODE = os.getenv("OMNIVORE_FAKE", "").strip() == "1" or not os.getenv("OMNIVORE_API_KEY")
IS_FAKE = _FAKE_MODE  # exported so callers can branch if needed

# Keep BASE/HEADERS importable everywhere
BASE = "https://api.omnivore.io/1.0"

def _embedded(obj, key):
    return ((obj or {}).get("_embedded") or {}).get(key, []) or []

def _now_iso():
    return datetime.utcnow().isoformat() + "Z"

# =====================================================================================
# FAKE IMPLEMENTATION  (shared JSON store so CLI + server see the same data)
# =====================================================================================
if _FAKE_MODE:
    # In fake mode, headers are inert (import-safe)
    HEADERS = {}

    # -------- Persistence paths (shared across processes) --------
    try:
        from django.conf import settings as _dj_settings
        _BASE_DIR = getattr(_dj_settings, "BASE_DIR", Path.cwd())
    except Exception:
        _BASE_DIR = Path.cwd()

    _FAKE_STORE_PATH = Path(os.getenv("OMNIVORE_FAKE_STORE", str(Path(_BASE_DIR) / "omnivore_fake_store.json")))

    def _load_store() -> dict:
        if _FAKE_STORE_PATH.exists():
            try:
                return json.loads(_FAKE_STORE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"locations": {}}

    def _save_store(db: dict) -> None:
        tmp = _FAKE_STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(_FAKE_STORE_PATH)

    # In-memory "DB" (loaded from disk at import)
    # locations: { location_id: { "seq": int, "tender_types": [...], "tickets": { id: ticket_dict } } }
    _DB: dict[str, dict] = _load_store()

    # Deterministic sample menu (ids/prices aligned to your examples)
    _FAKE_MENU = {
        "101": ("Pizza", 1699, {"2": 1499}),  # price_level "2" -> 14.99
        "200": ("Mozz Sticks", 799, {}),
        "201": ("Garlic Bread", 499, {}),
        "206": ("Wings (6)", 1099, {}),
        "207": ("Bruschetta", 899, {}),
    }

    def _seed_location(location_id: str):
        if location_id in _DB["locations"]:
            return
        rnd = random.Random(hash(location_id) & 0xFFFFFFFF)

        def make_ticket(idx: int):
            # small random open checks for demo browsing
            items = []
            menu_keys = list(_FAKE_MENU.keys())
            for _ in range(rnd.randint(2, 4)):
                mk = rnd.choice(menu_keys)
                name, base, levels = _FAKE_MENU[mk]
                qty = rnd.randint(1, 2)
                price = base
                items.append({
                    "id": f"itm_{idx}_{rnd.randint(1000,9999)}",
                    "menu_item": mk,
                    "name": name,
                    "quantity": qty,
                    "price": price,
                    "total": price * qty,
                    "seat": rnd.randint(1, 2),
                    "price_level": None,
                })
            subtotal = sum(i["total"] for i in items)
            tax = int(round(subtotal * 0.0825))
            total = subtotal + tax
            created = (datetime.utcnow() - timedelta(minutes=rnd.randint(8, 55))).isoformat() + "Z"
            num = 1000 + idx
            return {
                "id": f"tkt_{idx}_{rnd.randint(10000,99999)}",
                "ticket_number": num,
                "open": True,
                "created_at": created,
                "updated_at": created,
                "subtotal": subtotal,
                "tax": tax,
                "total": total,
                "paid": 0,
                "tip": 0,
                "employee": "100",
                "revenue_center": "1",
                "order_type": "2",
                "auto_send": True,
                "_embedded": {
                    "employee": {
                        "check_name": rnd.choice(["SAMPLE SERVER"]),
                        "first_name": rnd.choice(["Alex", "Sam", "Jordan", "Taylor", "Riley"]),
                        "last_name": rnd.choice(["M", "K", "P", "S", "D"]),
                    }
                },
                "items": items,
                "payments": [],
            }

        tickets = {t["id"]: t for t in [make_ticket(i) for i in range(1, rnd.randint(3, 6))]}
        last_ticket_number = max([t["ticket_number"] for t in tickets.values()]) if tickets else 1000

        _DB["locations"][location_id] = {
            "seq": last_ticket_number,  # for unique, monotonically increasing ticket numbers
            "tender_types": [
                {"id": "cash", "name": "Cash"},
                {"id": "credit_card", "name": "Credit Card"},
                {"id": "custom_dyne", "name": "Dyne (Custom Tender)"},
            ],
            "tickets": tickets,
        }
        _save_store(_DB)

    def _recompute_totals(t: dict):
        subtotal = sum(i["total"] for i in t.get("items", []))
        tax = int(round(subtotal * 0.0825))
        total = subtotal + tax
        t["subtotal"], t["tax"], t["total"] = subtotal, tax, total
        t["updated_at"] = _now_iso()

    def _maybe_close(t: dict):
        if int(t.get("paid", 0)) >= int(t.get("total", 0)):
            t["open"] = False
            t["updated_at"] = _now_iso()

    def _resolve_ticket_id(location_id: str, ticket_id_or_num: str) -> str:
        """
        In FAKE mode only: allow callers to pass either the internal ticket id ("tkt_...") or
        the public numeric ticket_number (e.g., "1006"). Returns the internal id.
        """
        _seed_location(location_id)
        s = str(ticket_id_or_num).strip()
        if s and s.isdigit():
            num = int(s)
            for _tid, _t in _DB["locations"][location_id]["tickets"].items():
                if int(_t.get("ticket_number", -1)) == num:
                    return _tid
            raise KeyError(f"Ticket with number {num} not found for location {location_id}")
        return s  # assume it's already an id

    # ---------------- Public API (fake) ----------------

    def list_open_tickets(location_id: str):
        _seed_location(location_id)
        open_tix = [t for t in _DB["locations"][location_id]["tickets"].values() if t.get("open")]
        for t in open_tix:
            emp = ((t.get("_embedded") or {}).get("employee") or {})
            t["match_text"] = " ".join(
                str(x or "") for x in [
                    t.get("id"),
                    t.get("ticket_number"),
                    emp.get("check_name"),
                    emp.get("first_name"),
                    emp.get("last_name"),
                ]
            ).lower()
        return [dict(t) for t in open_tix]

    def get_ticket(location_id: str, ticket_id: str):
        _seed_location(location_id)
        tid = _resolve_ticket_id(location_id, ticket_id)
        t = _DB["locations"][location_id]["tickets"].get(tid)
        if not t:
            # if unknown (deep-link), synthesize a small closed ticket
            loc = _DB["locations"][location_id]
            loc["seq"] = int(loc.get("seq") or 1000) + 1
            t = {
                "id": tid,
                "ticket_number": loc["seq"],
                "open": False,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "subtotal": 0,
                "tax": 0,
                "total": 0,
                "paid": 0,
                "tip": 0,
                "employee": "100",
                "revenue_center": "1",
                "order_type": "2",
                "auto_send": True,
                "_embedded": {"employee": {"check_name": "DINE-IN", "first_name": "Demo", "last_name": "Ticket"}},
                "items": [],
                "payments": [],
            }
            _DB["locations"][location_id]["tickets"][tid] = t
            _save_store(_DB)
        return dict(t)

    def get_ticket_items(location_id: str, ticket_id: str):
        _seed_location(location_id)
        tid = _resolve_ticket_id(location_id, ticket_id)
        t = _DB["locations"][location_id]["tickets"].get(tid) or {}
        return [dict(i) for i in t.get("items", [])]

    def get_ticket_payments(location_id: str, ticket_id: str):
        _seed_location(location_id)
        tid = _resolve_ticket_id(location_id, ticket_id)
        t = _DB["locations"][location_id]["tickets"].get(tid) or {}
        return [dict(p) for p in t.get("payments", [])]

    def list_tender_types(location_id: str):
        _seed_location(location_id)
        return [dict(tt) for tt in _DB["locations"][location_id]["tender_types"]]

    def create_payment_with_tender_type(
        location_id: str,
        ticket_id: str,
        amount_cents: int,
        *,
        tender_type_id: str | None,
        reference: str,
        tip_cents: int | None = None,
    ):
        _seed_location(location_id)
        tid = _resolve_ticket_id(location_id, ticket_id)
        t = _DB["locations"][location_id]["tickets"].get(tid)
        if not t:
            raise RuntimeError(f"Ticket {ticket_id} not found for location {location_id}")
        pay_id = f"pay_{int(time.time()*1000)}"
        payment = {
            "id": pay_id,
            "type": (tender_type_id or "cash"),
            "amount": int(amount_cents),
            "tip": int(tip_cents or 0),
            "reference": reference,
            "created_at": _now_iso(),
        }
        t.setdefault("payments", []).append(payment)
        t["paid"] = int(t.get("paid", 0)) + int(amount_cents)
        t["tip"] = int(t.get("tip", 0)) + int(tip_cents or 0)
        t["updated_at"] = _now_iso()
        _maybe_close(t)
        _save_store(_DB)
        return dict(payment)

    # Back-compat alias used by older code
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
        return create_payment_with_tender_type(
            location_id=location_id,
            ticket_id=ticket_id,
            amount_cents=amount_cents,
            tender_type_id=str(tender_type_id or "cash"),
            reference=reference,
            tip_cents=tip_cents,
        )

    def create_ticket(
        location_id: str,
        *,
        employee: str,
        revenue_center: str,
        order_type: str,
        auto_send: bool = True,
    ):
        _seed_location(location_id)
        loc = _DB["locations"][location_id]
        # unique, monotonic ticket numbers
        loc["seq"] = int(loc.get("seq") or 1000) + 1
        ticket_number = loc["seq"]
        tid = f"tkt_{ticket_number}_{random.randint(10000, 99999)}"
        created = _now_iso()
        ticket = {
            "id": tid,
            "ticket_number": ticket_number,
            "open": True,
            "created_at": created,
            "updated_at": created,
            "subtotal": 0,
            "tax": 0,
            "total": 0,
            "paid": 0,
            "tip": 0,
            "employee": str(employee),
            "revenue_center": str(revenue_center),
            "order_type": str(order_type),
            "auto_send": bool(auto_send),
            "_embedded": {
                "employee": {"check_name": "DINE-IN", "first_name": "Demo", "last_name": "User"}
            },
            "items": [],
            "payments": [],
        }
        loc["tickets"][tid] = ticket
        _save_store(_DB)
        return dict(ticket)

    def add_items(location_id: str, ticket_id: str, *, items: list[dict]):
        _seed_location(location_id)
        tid = _resolve_ticket_id(location_id, ticket_id)
        t = _DB["locations"][location_id]["tickets"].get(tid)
        if not t:
            raise RuntimeError(f"Ticket {ticket_id} not found for location {location_id}")

        added = []
        for raw in items or []:
            mid = str(raw.get("menu_item"))
            qty = int(raw.get("quantity") or 1)
            level = str(raw.get("price_level")) if raw.get("price_level") is not None else None

            if mid in _FAKE_MENU:
                name, base, levels = _FAKE_MENU[mid]
                price = int(levels.get(level, base))
            else:
                name, base, price = (f"Item {mid}", 999, 999)

            line_total = int(price) * qty
            itm = {
                "id": f"itm_{len(t['items'])+1}",
                "menu_item": mid,
                "name": name,
                "quantity": qty,
                "price": int(price),       # per-unit cents
                "total": line_total,       # line total cents
                "seat": raw.get("seat", 1),
                "price_level": level,
            }
            t["items"].append(itm)
            added.append(itm)

        _recompute_totals(t)
        _save_store(_DB)
        return {"_embedded": {"items": [dict(i) for i in added]}}

# =====================================================================================
# REAL IMPLEMENTATION (pass-through to Omnivore)
# =====================================================================================
else:
    import requests
    from decouple import config

    API_KEY = config("OMNIVORE_API_KEY")
    HEADERS = {"Api-Key": API_KEY}

    def _err_blob(resp):
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, resp.text

    def list_open_tickets(location_id: str):
        url = f"{BASE}/locations/{location_id}/tickets"
        params = {"where": "eq(open,true)"}
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        tickets = _embedded(r.json(), "tickets")
        open_tix = [t for t in tickets if t.get("open") is True]
        for t in open_tix:
            emp = ((t.get("_embedded") or {}).get("employee") or {})
            t["match_text"] = " ".join(
                str(x or "") for x in [
                    t.get("id"),
                    t.get("ticket_number"),
                    emp.get("check_name"),
                    emp.get("first_name"),
                    emp.get("last_name"),
                ]
            ).lower()
        return open_tix

    def get_ticket(location_id: str, ticket_id: str):
        # REAL API expects an internal ID, not the numeric number
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
        url = f"{BASE}/locations/{location_id}/tender_types"
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return _embedded(r.json(), "tender_types")

    def create_payment_with_tender_type(
        location_id: str,
        ticket_id: str,
        amount_cents: int,
        *,
        tender_type_id: str | None,
        reference: str,
        tip_cents: int | None = None,
    ):
        url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/payments"
        body = {
            "type": "cash",
            "amount": int(amount_cents),
            "tip": int(tip_cents or 0),
        }
        r = requests.post(url, json=body, headers={**HEADERS, "Content-Type": "application/json"}, timeout=10)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text}
        if not r.ok:
            raise RuntimeError(f"Omnivore {r.status_code} {url} -> {payload}")
        return payload

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
        if tender_type_id is None:
            tender_type_id = _cfg("OMNIVORE_TENDER_TYPE_ID", default="100")
        return create_payment_with_tender_type(
            location_id=location_id,
            ticket_id=ticket_id,
            amount_cents=amount_cents,
            tender_type_id=str(tender_type_id),
            reference=reference,
            tip_cents=tip_cents,
        )

    def create_ticket(
        location_id: str,
        *,
        employee: str,
        revenue_center: str,
        order_type: str,
        auto_send: bool = True,
    ):
        url = f"{BASE}/locations/{location_id}/tickets"
        payload = {
            "employee": str(employee),
            "revenue_center": str(revenue_center),
            "order_type": str(order_type),
            "auto_send": bool(auto_send),
        }
        r = requests.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=15)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        if not r.ok:
            raise RuntimeError(f"Omnivore {r.status_code} {url} -> {body}")
        return body

    def add_items(location_id: str, ticket_id: str, *, items: list[dict]):
        url = f"{BASE}/locations/{location_id}/tickets/{ticket_id}/items"
        payload = {"items": items or []}
        r = requests.post(url, json=payload, headers={**HEADERS, "Content-Type": "application/json"}, timeout=15)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        if not r.ok:
            raise RuntimeError(f"Omnivore {r.status_code} {url} -> {body}")
        return body
