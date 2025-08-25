from twilio.rest import Client
from django.conf import settings
from decouple import config

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