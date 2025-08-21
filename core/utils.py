from twilio.rest import Client
from django.conf import settings
from decouple import config

ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = config("TWILIO_AUTH_TOKEN")
VERIFY_SID  = config("TWILIO_VERIFY_SERVICE_SID")

_client = Client(config("TWILIO_ACCOUNT_SID"), config("TWILIO_AUTH_TOKEN"))

def send_otp(phone: str):
    """
    Initiate a Verify SMS to the phone in E.164 format, e.g. +15551234567
    """
    return _client.verify.v2.services(VERIFY_SID).verifications.create(
        to=phone, channel="sms"
    )

def check_otp(phone: str, code: str):
    """
    Check a user-supplied code against Verify.
    Returns verification_check.status (approved, pending) and validity.
    """
    vc = _client.verify.v2.services(VERIFY_SID).verification_checks.create(
        to=phone, code=code
    )
    return vc.status  # 'approved' when correct

def to_e164_us(raw: str) -> str:
    s = "".join(ch for ch in raw if ch.isdigit())   # strip spaces, dashes, ()
    if s.startswith("1") and len(s) == 11:
        return f"+{s}"
    if len(s) == 10:  # assume US
        return f"+1{s}"
    if raw.startswith("+") and raw[1:].isdigit():
        return raw
    raise ValueError("Invalid US phone number")