from twilio.rest import Client
from django.conf import settings
from decouple import config
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from .constants import CUSTOMER_SSR


ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = config("TWILIO_AUTH_TOKEN")
VERIFY_SID  = config("TWILIO_VERIFY_SERVICE_SID")

_client = Client(config("TWILIO_ACCOUNT_SID"), config("TWILIO_AUTH_TOKEN"))

def send_sms_otp(phone_e164: str):
    return _client.verify.v2.services(VERIFY_SID).verifications.create(
        to=phone_e164, channel="sms"
    )

def check_sms_otp(phone_e164: str, code: str) -> str:
    vc = _client.verify.v2.services(VERIFY_SID).verification_checks.create(
        to=phone_e164, code=code
    )
    return vc.status  # 'approved' on success

# NEW — email channel
def send_email_otp(email: str):
    return _client.verify.v2.services(VERIFY_SID).verifications.create(
        to=email, channel="email"
    )

def check_email_otp(email: str, code: str) -> str:
    vc = _client.verify.v2.services(VERIFY_SID).verification_checks.create(
        to=email, code=code
    )
    return vc.status

def to_e164_us(raw: str) -> str:
    s = "".join(ch for ch in raw if ch.isdigit())   # strip spaces, dashes, ()
    if s.startswith("1") and len(s) == 11:
        return f"+{s}"
    if len(s) == 10:  # assume US
        return f"+1{s}"
    if raw.startswith("+") and raw[1:].isdigit():
        return raw
    raise ValueError("Invalid US phone number")

# core/utils_email.py

API_SENDGRID = config("API_SENDGRID")
FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="no-reply@example.com")

def send_manager_invite_email(to_email: str, invite_link: str, restaurant_name: str, expires_at) -> None:
    """
    Minimal, production-safe SendGrid send.
    Raises on errors so caller can decide response behavior.
    """
    if not API_SENDGRID:
        raise RuntimeError("Missing API_SENDGRID")

    subject = f"You’re invited to manage {restaurant_name}"
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.5;color:#0f172a">
      <h2 style="margin:0 0 12px">You’re invited as a manager</h2>
      <p>You’ve been invited to manage <strong>{restaurant_name}</strong> on Dine N Dash.</p>
      <p>
        Click the button below to accept and set your password. This link expires on
        <strong>{expires_at:%Y-%m-%d %H:%M}</strong>.
      </p>
      <p style="margin:20px 0">
        <a href="{invite_link}" style="background:#0f172a;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;display:inline-block">
          Accept Invite
        </a>
      </p>
      <p>If the button doesn’t work, paste this URL into your browser:<br>
      <a href="{invite_link}">{invite_link}</a></p>
      <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
      <p style="font-size:12px;color:#64748b">If you didn’t expect this invite, you can ignore this email.</p>
    </div>
    """

    sg = SendGridAPIClient(API_SENDGRID)
    msg = Mail(from_email=FROM_EMAIL, to_emails=to_email, subject=subject, html_content=html)
    resp = sg.send(msg)
    # Non-2xx? raise with details so you see why
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"SendGrid failed: {resp.status_code} {resp.body}")


TWILIO_FROM_NUMBER = config("TWILIO_FROM_NUMBER", default="")

def send_sms(to_number: str, body: str) -> dict:
    """
    Send an SMS via Twilio. If Twilio env vars are missing, no-op gracefully.
    Returns: {"ok": bool, "sid": str|None, "error": str|None}
    """
    if not (ACCOUNT_SID and AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return {"ok": False, "sid": None, "error": "Twilio not configured"}

    try:
        # Local import so the project doesn't require twilio unless used
        from twilio.rest import Client
    except Exception as e:
        return {"ok": False, "sid": None, "error": f"Twilio SDK not installed: {e}"}

    try:
        client = Client(ACCOUNT_SID, AUTH_TOKEN)
        msg = client.messages.create(
            to='+18777804236',
            from_=TWILIO_FROM_NUMBER,
            body=body,
        )
        return {"ok": True, "sid": msg.sid, "error": None}
    except Exception as e:
        return {"ok": False, "sid": None, "error": str(e)}

# core/stripe_utils.py
import stripe
from django.conf import settings

stripe.api_key = config('STRIPE_SK')

def ensure_stripe_customer_by_email(email: str, metadata: dict | None = None) -> str:
    """
    Create (or reuse) a Stripe Customer by email for a *pending* signup.
    You can simply always create a new one; re-use is optional.
    """
    customer = stripe.Customer.create(
        email=email or None,
        metadata=metadata or {},
    )
    return customer.id

def create_setup_intent_for_customer(customer_id: str) -> stripe.SetupIntent:
    return stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=["card"],
        usage="off_session",
    )

def seed_pending_card_session(request, *, user, phone_e164: str):
    """
    Prime the signup session so the existing /add-card -> /set-pin -> save_pin_finalize
    pipeline can run for Google OAuth users as well.
    """
    ss = {
        "email": (getattr(user, "email", "") or "").strip().lower(),
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "phone": phone_e164 or "",
        "email_verified": True,     # OAuth email is trusted
        "phone_verified": True,     # we just OTP-verified it
        "stage": "need_card",       # gate that /add-card checks
    }
    request.session[CUSTOMER_SSR] = ss
    request.session.modified = True

def send_staff_invite_email(to_email: str, invite_link: str, restaurant_name: str, expires_at) -> None:
    """
    Minimal, production-safe SendGrid send.
    Raises on errors so caller can decide response behavior.
    """
    if not API_SENDGRID:
        raise RuntimeError("Missing API_SENDGRID")

    subject = f"Staff Invite for {restaurant_name}"
    html = f"""
    <div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.5;color:#0f172a">
      <h2 style="margin:0 0 12px">You’re invited as a manager</h2>
      <p>You’ve been invited to <strong>{restaurant_name}</strong> on Dine N Dash.</p>
      <p>
        Click the button below to accept and set your password. This link expires on
        <strong>{expires_at:%Y-%m-%d %H:%M}</strong>.
      </p>
      <p style="margin:20px 0">
        <a href="{invite_link}" style="background:#0f172a;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;display:inline-block">
          Accept Invite
        </a>
      </p>
      <p>If the button doesn’t work, paste this URL into your browser:<br>
      <a href="{invite_link}">{invite_link}</a></p>
      <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb">
      <p style="font-size:12px;color:#64748b">If you didn’t expect this invite, you can ignore this email.</p>
    </div>
    """

    sg = SendGridAPIClient(API_SENDGRID)
    msg = Mail(from_email=FROM_EMAIL, to_emails=to_email, subject=subject, html_content=html)
    resp = sg.send(msg)
    # Non-2xx? raise with details so you see why
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"SendGrid failed: {resp.status_code} {resp.body}")