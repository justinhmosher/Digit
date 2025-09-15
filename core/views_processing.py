# core/payments.py
from __future__ import annotations

import os
import json
import hashlib
import uuid
from typing import Optional, Dict

import stripe


# --- Stripe setup ---
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# --- Errors ---
class PaymentError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        decline_code: str | None = None,
        payment_intent_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.decline_code = decline_code
        self.payment_intent_id = payment_intent_id


# --- Utils ---
def ensure_stripe_key():
    if not getattr(stripe, "api_key", None):
        raise PaymentError("Stripe secret key missing (set STRIPE_SECRET_KEY).")


def build_idem_key(prefix: str, payload: Dict) -> str:
    """
    Deterministic idempotency key from the exact params sent to Stripe.
    Ensures same params -> same key (safe retry); changed params -> new key.
    """
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha1(normalized).hexdigest()[:16]
    return f"{prefix}:{digest}"


# --- Main helpers ---
def charge_customer_off_session(
    *,
    customer_id: str,
    payment_method_id: str,
    amount_cents: int,
    currency: str = "usd",
    description: str = "",
    idempotency_key: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> stripe.PaymentIntent:
    """
    Creates & confirms a PaymentIntent off-session against a saved PM.
    Returns PaymentIntent on success, raises PaymentError on failure.
    Retries once with a fresh idempotency key iff Stripe returns the
    'idempotent key used with different params' error.
    """
    ensure_stripe_key()

    # Build the exact request dict we’ll send to Stripe
    request_payload = {
        "amount": int(amount_cents),
        "currency": currency,
        "customer": customer_id,
        "payment_method": payment_method_id,
        "payment_method_types": ["card"],   # explicit because we pass a PM
        "confirm": True,
        "off_session": True,
        "description": (description or None),
        "metadata": (metadata or {}),
    }

    # Default idempotency key if not provided
    idem_key = idempotency_key or build_idem_key("close", request_payload)

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
        # Handle the exact idempotency-mismatch case with one retry
        msg = e.user_message or str(e)
        if "Keys for idempotent requests can only be used with the same parameters" in msg:
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

    # Sanity status check
    if intent.status not in ("succeeded", "requires_capture", "processing"):
        raise PaymentError(f"unexpected_intent_status: {intent.status}", payment_intent_id=intent.id)

    return intent


def refund_payment_intent(intent_id: str, reason: Optional[str] = None) -> stripe.Refund:
    ensure_stripe_key()
    try:
        return stripe.Refund.create(payment_intent=intent_id, reason=reason or None)
    except stripe.error.StripeError as e:
        raise PaymentError(f"refund_failed: {e.user_message or str(e)}")

