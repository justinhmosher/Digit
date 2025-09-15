# core/payments.py
from __future__ import annotations

import os
from typing import Optional, Dict
import uuid
import stripe
import json
import hashlib


import stripe


# --- Stripe setup ---
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


class PaymentError(Exception):
    def __init__(self, msg, *, code=None, decline_code=None, payment_intent_id=None):
        super().__init__(msg)
        self.code = code
        self.decline_code = decline_code
        self.payment_intent_id = payment_intent_id

def ensure_stripe_key():
    if not getattr(stripe, "api_key", None):
        raise RuntimeError("Stripe secret key not configured")

def build_idem_key(prefix: str, payload: Dict) -> str:
    # Whatever you already use; keeping simple & deterministic
    return f"{prefix}:{hash(frozenset(payload.items()))}"

def charge_customer_off_session(
    *,
    customer_id: str,
    payment_method_id: str,
    amount_cents: int,
    currency: str = "usd",
    description: str = "",
    idempotency_key: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
    # --- NEW: Connect options ---
    destination_account_id: Optional[str] = None,   # acct_...
    on_behalf_of: Optional[str] = None,             # usually same acct_...
    application_fee_amount: Optional[int] = None,   # in cents; optional
) -> stripe.PaymentIntent:
    """
    Creates & confirms an off-session PaymentIntent against a saved card.
    Supports Stripe Connect destination charges via transfer_data / on_behalf_of.
    Returns the PaymentIntent on success or raises PaymentError on failure.
    """
    ensure_stripe_key()

    # Base payload
    request_payload: Dict = {
        "amount": int(amount_cents),
        "currency": currency,
        "customer": customer_id,
        "payment_method": payment_method_id,
        "payment_method_types": ["card"],
        "confirm": True,
        "off_session": True,
        "description": (description or None),
        "metadata": (metadata or {}),
    }

    # ---- Connect routing (destination charge / direct-on-behalf) ----
    # Destination charge (funds to connected account, card present on platform):
    if destination_account_id:
        request_payload["transfer_data"] = {"destination": destination_account_id}

    # Tell Stripe this PI is on behalf of the connected account (optional but recommended)
    if on_behalf_of:
        request_payload["on_behalf_of"] = on_behalf_of

    # Platform fee (optional, only if your platform takes a fee)
    if application_fee_amount is not None:
        request_payload["application_fee_amount"] = int(application_fee_amount)

    # Stable idem key (include connect bits so params stay consistent)
    idem_key = idempotency_key or build_idem_key("close", {
        "amount": request_payload["amount"],
        "customer": request_payload["customer"],
        "pm": request_payload["payment_method"],
        "dest": destination_account_id or "",
        "oba": on_behalf_of or "",
        "fee": request_payload.get("application_fee_amount", 0),
        "currency": currency,
        "desc": description or "",
    })

    def _create_with_key(_key: str) -> stripe.PaymentIntent:
        return stripe.PaymentIntent.create(idempotency_key=_key, **request_payload)

    try:
        intent = _create_with_key(idem_key)
    except stripe.error.CardError as e:
        pi = (getattr(e, "json_body", None) or {}).get("error", {}).get("payment_intent", {}) or {}
        raise PaymentError(
            f"card_error: {e.user_message or str(e)}",
            code=getattr(e, "code", None),
            decline_code=getattr(e, "decline_code", None),
            payment_intent_id=pi.get("id"),
        )
    except stripe.error.StripeError as e:
        msg = e.user_message or str(e)
        if "Keys for idempotent requests can only be used with the same parameters" in msg:
            # single safe retry with a fresh key
            try:
                fresh_key = f"{idem_key}:r:{uuid.uuid4().hex[:8]}"
                intent = _create_with_key(fresh_key)
            except stripe.error.CardError as e2:
                pi = (getattr(e2, "json_body", None) or {}).get("error", {}).get("payment_intent", {}) or {}
                raise PaymentError(
                    f"card_error: {e2.user_message or str(e2)}",
                    code=getattr(e2, "code", None),
                    decline_code=getattr(e2, "decline_code", None),
                    payment_intent_id=pi.get("id"),
                )
            except stripe.error.StripeError as e2:
                pi = (getattr(e2, "json_body", None) or {}).get("error", {}).get("payment_intent", {}) or {}
                raise PaymentError(
                    f"stripe_error: {e2.user_message or str(e2)}",
                    code=getattr(e2, "code", None),
                    payment_intent_id=pi.get("id"),
                )
        else:
            pi = (getattr(e, "json_body", None) or {}).get("error", {}).get("payment_intent", {}) or {}
            raise PaymentError(
                f"stripe_error: {msg}",
                code=getattr(e, "code", None),
                payment_intent_id=pi.get("id"),
            )

    if intent.status not in ("succeeded", "requires_capture", "processing"):
        raise PaymentError(f"unexpected_intent_status: {intent.status}", payment_intent_id=intent.id)

    return intent

def refund_payment_intent(intent_id: str, reason: Optional[str] = None) -> stripe.Refund:
    ensure_stripe_key()
    try:
        return stripe.Refund.create(payment_intent=intent_id, reason=reason or None)
    except stripe.error.StripeError as e:
        raise PaymentError(f"refund_failed: {e.user_message or str(e)}")