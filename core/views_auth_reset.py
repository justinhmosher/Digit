from __future__ import annotations
import re
from typing import Optional

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core.models import CustomerProfile
# ⬇️ adjust import path if your utils live elsewhere
from core.utils import (
    send_sms_otp, check_sms_otp,
    send_email_otp, check_email_otp, to_e164_us
)



def _is_email(s: str) -> bool:
    try:
        validate_email((s or "").strip())
        return True
    except ValidationError:
        return False


def _find_user_by_identifier(identifier: str) -> Optional[User]:
    """
    Try email match first; if not, treat as phone and match CustomerProfile.phone (digits only).
    """
    ident = (identifier or "").strip()
    if _is_email(ident):
        return User.objects.filter(email__iexact=ident).first()

    phone_e164 = to_e164_us(ident)
    if not phone_e164:
        return None

    # store phone in DB as digits or E.164? If digits, strip here too:
    digits = re.sub(r"\D+", "", phone_e164)
    prof = CustomerProfile.objects.filter(phone__in=[phone_e164, digits]).select_related("user").first()
    return prof.user if prof else None


def _sess_set(request: HttpRequest, **kv):
    request.session.update(kv)
    request.session.modified = True


def _sess_get(request: HttpRequest, key: str, default=None):
    return request.session.get(key, default)


# ---------- 1) start: send OTP ----------
@ensure_csrf_cookie
@csrf_protect
@require_POST
def reset_start(request: HttpRequest) -> JsonResponse:
    identifier = (request.POST.get("identifier") or "").strip()
    if not identifier:
        return JsonResponse({"ok": False, "error": "missing_identifier"}, status=400)

    user = _find_user_by_identifier(identifier)

    # Save only what we need for subsequent steps.
    # Don't reveal whether the account exists.
    _sess_set(request, pwd_reset_ident=identifier, pwd_reset_user_id=(user.id if user else None))
    _sess_set(request, pwd_reset_otp_ok=False, pwd_reset_pin_ok=False)

    # Kick off Twilio Verify to email or phone
    try:
        if _is_email(identifier):
            send_email_otp(identifier)
        else:
            phone_e164 = to_e164_us(identifier)
            if not phone_e164:
                # Still respond generic success to avoid leaking; client will fail at verify.
                return JsonResponse({"ok": True})
            send_sms_otp(phone_e164)
    except Exception as e:
        # Still return generic OK to avoid enumeration; log on server if needed.
        print("[reset_start] Twilio send error:", e)

    return JsonResponse({"ok": True})


# ---------- 2) verify OTP ----------
@csrf_protect
@require_POST
def reset_verify(request: HttpRequest) -> JsonResponse:
    ident = _sess_get(request, "pwd_reset_ident")
    if not ident:
        return JsonResponse({"ok": False, "error": "no_reset_in_progress"}, status=400)

    code = (request.POST.get("otp") or "").strip()
    if not code:
        return JsonResponse({"ok": False, "error": "missing_otp"}, status=400)

    try:
        status = (
            check_email_otp(ident, code)
            if _is_email(ident)
            else check_sms_otp(to_e164_us(ident) or "", code)
        )
    except Exception as e:
        print("[reset_verify] Twilio check error:", e)
        return JsonResponse({"ok": False, "error": "otp_check_failed"}, status=400)

    if status != "approved":
        return JsonResponse({"ok": False, "error": "bad_otp"}, status=400)

    _sess_set(request, pwd_reset_otp_ok=True)
    return JsonResponse({"ok": True})


# ---------- 3) verify PIN ----------
@csrf_protect
@require_POST
def reset_pin(request: HttpRequest) -> JsonResponse:
    if not _sess_get(request, "pwd_reset_otp_ok"):
        return JsonResponse({"ok": False, "error": "sequence_error"}, status=400)

    user_id = _sess_get(request, "pwd_reset_user_id")
    if not user_id:
        # identifier didn’t map to a user in start()
        return JsonResponse({"ok": False, "error": "unknown_account"}, status=400)

    pin = (request.POST.get("pin") or "").strip()
    if not pin:
        return JsonResponse({"ok": False, "error": "missing_pin"}, status=400)

    prof = CustomerProfile.objects.filter(user_id=user_id).first()
    if not prof or not prof.pin_hash or not check_password(pin, prof.pin_hash):
        return JsonResponse({"ok": False, "error": "bad_pin"}, status=400)

    _sess_set(request, pwd_reset_pin_ok=True)
    return JsonResponse({"ok": True})


# ---------- 4) finalize (set new password) ----------
@csrf_protect
@require_POST
def reset_finalize(request: HttpRequest) -> JsonResponse:
    if not _sess_get(request, "pwd_reset_pin_ok"):
        return JsonResponse({"ok": False, "error": "sequence_error"}, status=400)

    user_id = _sess_get(request, "pwd_reset_user_id")
    user = User.objects.filter(id=user_id).first()
    if not user:
        return JsonResponse({"ok": False, "error": "unknown_account"}, status=400)

    p1 = request.POST.get("password1") or ""
    p2 = request.POST.get("password2") or ""
    if p1 != p2 or len(p1) < 8:
        return JsonResponse({"ok": False, "error": "password_invalid"}, status=400)

    user.set_password(p1)
    user.save(update_fields=["password"])

    # clean up session
    for k in ("pwd_reset_ident", "pwd_reset_user_id", "pwd_reset_otp_ok", "pwd_reset_pin_ok"):
        request.session.pop(k, None)
    request.session.modified = True

    return JsonResponse({"ok": True})
