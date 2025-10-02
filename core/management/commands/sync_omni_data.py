# core/management/commands/sync_omnivore_cache.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import RestaurantProfile
from core.omnivore import BASE, HEADERS
import requests
from decimal import Decimal, InvalidOperation

def _to_cents(val) -> int:
    """
    Accepts:
      - None                           -> 0
      - int cents (e.g., 500)         -> 500 (if <100 treat as dollars → *100)
      - float/str dollars ("5.00")    -> 500
      - str "500" (cents-like)        -> 500
    """
    if val is None:
        return 0
    if isinstance(val, int):
        return val if val >= 100 else val * 100
    try:
        d = Decimal(str(val))
        s = str(val)
        if "." in s or d < 100:
            return int((d * 100).quantize(Decimal("1")))
        return int(d)
    except (InvalidOperation, ValueError):
        return 0

def _extract_price_cents_from_item(it) -> int:
    """
    Return the *best non-zero* price (in cents) we can find for a menu item.
    Searches common top-level fields and all price levels; prefers defaults.
    """
    candidates = []

    # common top-level fields
    for key in ("price_per_unit", "price", "default_price", "base_price"):
        if key in it:
            candidates.append(_to_cents(it.get(key)))

    # price levels
    try:
        levels = (it.get("_embedded", {}) or {}).get("price_levels", []) or []
        default_levels = [lvl for lvl in levels if lvl.get("is_default")]
        for lvl in default_levels:
            candidates.append(_to_cents(lvl.get("price_per_unit") or lvl.get("price")))
        for lvl in levels:
            candidates.append(_to_cents(lvl.get("price_per_unit") or lvl.get("price")))
    except Exception:
        pass

    # sometimes in "pricing"
    try:
        pricing = it.get("pricing") or {}
        candidates.append(_to_cents(pricing.get("amount")))
        candidates.append(_to_cents(pricing.get("price")))
    except Exception:
        pass

    non_zero = [c for c in candidates if c and c > 0]
    return max(non_zero) if non_zero else 0


class Command(BaseCommand):
    help = "Sync Omnivore menu and staff caches into RestaurantProfile.*_cache fields"

    def handle(self, *args, **opts):
        total_items, total_staff = 0, 0

        qs = RestaurantProfile.objects.exclude(omnivore_location_id="").exclude(omnivore_location_id__isnull=True)
        for rp in qs:
            loc_id = (rp.omnivore_location_id or "").strip()
            if not loc_id:
                continue

            # -------- MENU --------
            menu_items = []
            try:
                url_items = f"{BASE}/locations/{loc_id}/menu/items/"
                resp = requests.get(url_items, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                payload = resp.json() or {}

                for it in (payload.get("_embedded", {}).get("menu_items", []) or []):
                    item_id = it.get("id")
                    if not item_id:
                        continue

                    price_cents = _extract_price_cents_from_item(it)

                    # category (best-effort)
                    category = ""
                    try:
                        cats = (it.get("_embedded", {}) or {}).get("menu_categories", []) or []
                        if cats:
                            category = cats[0].get("name") or ""
                    except Exception:
                        pass

                    menu_items.append({
                        "id": str(item_id),
                        "name": it.get("name") or "",
                        "price_cents": int(price_cents or 0),
                        "category": category,
                        "in_stock": it.get("in_stock"),
                    })

                rp.menu_cache = menu_items
                rp.menu_cache_synced_at = timezone.now()
                total_items += len(menu_items)

            except Exception as e:
                self.stderr.write(self.style.WARNING(f"[{rp.display_name()}] Menu sync failed: {e}"))

            # -------- STAFF --------
            staff = []
            try:
                url_emp = f"{BASE}/locations/{loc_id}/employees/"
                resp = requests.get(url_emp, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                payload = resp.json() or {}
                for emp in (payload.get("_embedded", {}).get("employees", []) or []):
                    staff.append({
                        "id": str(emp.get("id") or ""),
                        "name": emp.get("name") or "",
                        "check_name": emp.get("check_name") or "",
                        "role": emp.get("role") or "",
                        "is_active": bool(emp.get("active", True)),
                    })
                rp.staff_cache = staff
                rp.staff_cache_synced_at = timezone.now()
                total_staff += len(staff)
            except Exception as e:
                self.stderr.write(self.style.WARNING(f"[{rp.display_name()}] Staff sync failed: {e}"))

            rp.save(update_fields=["menu_cache", "menu_cache_synced_at", "staff_cache", "staff_cache_synced_at"])

        self.stdout.write(self.style.SUCCESS(f"Menu items synced: {total_items} · Staff synced: {total_staff}"))

