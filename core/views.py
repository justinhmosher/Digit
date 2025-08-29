from Digit import settings
from django.shortcuts import redirect, render, get_object_or_404
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
import win32com.client as win32
import pythoncom
import smtplib
from django.urls import reverse
from . tokens import generate_token
from django.contrib.sites.shortcuts import get_current_site
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.template.loader import render_to_string
from django.utils.encoding import force_str
from .models import RestaurantProfile, ManagerProfile, ManagerInvite, PhoneOTP, CustomerProfile
import requests
from decouple import config
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.db.models import Count,F,ExpressionWrapper,fields
from datetime import datetime
from itertools import chain
from collections import defaultdict
from datetime import datetime
import json, random
from django.views.decorators.http import require_POST, require_http_methods
from django.utils import timezone
from django.conf import settings
from .utils import send_sms_otp, to_e164_us, check_sms_otp, send_email_otp, check_email_otp, send_manager_invite_email
from allauth.socialaccount.models import SocialLogin
from allauth.socialaccount.helpers import complete_social_login
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.account.utils import perform_login
from django.contrib.auth.hashers import make_password
from django.views.decorators.csrf import ensure_csrf_cookie

def debug_session(request):
    return JsonResponse({"keys": list(request.session.keys())}, safe=False)

def homepage(request):
	return render(request,"core/homepage.html")

def _generate_code(n=6):
    return "".join(str(random.randint(0,9)) for _ in range(n))


@require_POST
def precheck_user_api(request):
    """
    POST JSON: { "email": "<email>" }
    Returns:
      { ok: true, exists: bool, has_verified_phone: bool, first_name, last_name }
    - exists=True if a Django User with this email exists
    - has_verified_phone=True if any attached profile shows a verified phone
    """
    import json
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "Bad JSON."}, status=400)

    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    user = User.objects.filter(email=email).first()
    exists = bool(user)

    has_verified_phone = False
    first_name = ""
    last_name = ""
    if user:
        first_name = user.first_name or ""
        last_name  = user.last_name or ""
        # Check any profile you maintain for a verified phone flag
        for prof_model in (CustomerProfile, OwnerProfile, ManagerProfile):
            prof = prof_model.objects.filter(user=user).first()
            if prof and getattr(prof, "phone_verified", False):
                has_verified_phone = True
                break

    return JsonResponse({
        "ok": True,
        "exists": exists,
        "has_verified_phone": has_verified_phone,
        "first_name": first_name,
        "last_name": last_name,
    })


# -------------------------
# CUSTOMER EMAIL-FIRST FLOW
# -------------------------
CUSTOMER_SSR = "customer_signup"

def signup(request):
    if request.method != "POST":
        return render(request, "core/signup.html")

    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        data = request.POST

    email = (data.get('email') or "").strip().lower()
    phone_raw = (data.get('phone') or "").strip()
    password1 = data.get('password1') or ""
    password2 = data.get('password2') or ""
    next_url = request.GET.get('next') or "/"

    if not email or not phone_raw:
        return JsonResponse({"ok": False, "error": "Email and phone are required."}, status=400)
    if password1 != password2:
        return JsonResponse({"ok": False, "error": "Passwords didn't match!"}, status=400)

    try:
        phone_e164 = to_e164_us(phone_raw)  # or replace with a full E.164 normalizer if you want intl later
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    # Create/update inactive user
    user = User.objects.filter(email=email).first()
    if user:
        if user.is_active:
            return JsonResponse({"ok": False, "error": "Email already registered with an active account."}, status=400)
        user.username = email
        user.email = email
        user.set_password(password1)
        user.is_active = False
        user.save()
    else:
        user = User.objects.create_user(username=email, email=email, password=password1)
        user.is_active = False
        user.save()

    profile, _ = CustomerProfile.objects.get_or_create(user=user)
    profile.phone = phone_e164
    # optional: track flags if you added them
    # profile.phone_verified = False
    # profile.email_verified = False
    try:
        profile.save()
    except Exception:
        return JsonResponse({"ok": False, "error": "Phone already in use."}, status=400)

    # Send phone OTP via Verify
    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    # IMPORTANT: return normalized phone so the client uses the same value
    return JsonResponse({"ok": True, "message": "OTP sent", "phone_e164": phone_e164, "next": next_url})

# Session bucket for multi-step customer signup
CUSTOMER_SSR = "customer_signup"

def _get_verified_phone_for_user(user):
    """
    Try to find a verified phone from any role profile tied to this user.
    Order of preference: Customer -> Owner -> Manager.
    Returns a raw phone string (e.g., '+18055551234') or None.
    """
    # Import here or at top, depending on your style
    from .models import CustomerProfile, OwnerProfile, ManagerProfile

    # helper to check a profile for a verified phone
    def pick(profile):
        if not profile:
            return None
        phone = getattr(profile, "phone", None)
        if not phone:
            return None
        # If the model has a phone_verified flag, require True; otherwise accept phone.
        has_flag = hasattr(profile, "phone_verified")
        if has_flag and not getattr(profile, "phone_verified", False):
            return None
        return phone

    # Try attached one-to-one attributes first (fast), then fallback query
    # Customer
    cp = getattr(user, "customerprofile", None)
    phone = pick(cp) or pick(CustomerProfile.objects.filter(user=user).first())
    if phone:
        return phone

    # Owner
    op = getattr(user, "ownerprofile", None)
    phone = pick(op) or pick(OwnerProfile.objects.filter(user=user).first())
    if phone:
        return phone

    # Manager
    mp = getattr(user, "managerprofile", None)
    phone = pick(mp) or pick(ManagerProfile.objects.filter(user=user).first())
    if phone:
        return phone

    return None

@require_POST
def customer_precheck_api(request):
    import json
    data = json.loads(request.body.decode() or "{}")
    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    # Block if a CustomerProfile already exists
    if CustomerProfile.objects.filter(user__email=email).exists():
        messages.error(request, "You already have a customer account.  Please sign in.")
        return JsonResponse({
            "ok":False,
            "redirect":reverse("core:signin")
            },status = 409)

    user = User.objects.filter(email=email).first()
    has_verified_phone = bool(user and _get_verified_phone_for_user(user))
    return JsonResponse({
        "ok": True,
        "exists": bool(user),
        "has_verified_phone": has_verified_phone
    })

@require_POST
def customer_begin_api(request):
    """
    NEW endpoint for the customer page's second step.
    Accepts:
      - existing user path: {email, phone?}
      - new user path: {email, first_name, last_name, phone, password1, password2}
    Decides which path based on whether User(email) exists.
    Sends SMS OTP and stashes a session bundle.
    """
    import json
    data = json.loads(request.body.decode() or "{}")

    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    user = User.objects.filter(email=email).first()
    if CustomerProfile.objects.filter(user__email=email).exists():
        return JsonResponse({
            "ok": False,
            "error": "You already have a customer account. Please sign in.",
            "signin_url": reverse("core:signin")
        }, status=409)
    is_existing = bool(user)

    # Decide phone source:
    phone_raw = (data.get("phone") or "").strip()
    if is_existing and not phone_raw:
        phone_raw = _get_verified_phone_for_user(user) or ""

    if not phone_raw:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

    # Normalize phone
    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    # Validate fields for NEW users
    first_name = (data.get("first_name") or "").strip()
    last_name  = (data.get("last_name") or "").strip()
    p1 = data.get("password1") or ""
    p2 = data.get("password2") or ""

    need_email_otp = False
    if not is_existing:
        # For new users we need first/last/passwords
        if not (first_name and last_name and p1 and p2):
            return JsonResponse({"ok": False, "error": "Please fill all fields."}, status=400)
        if p1 != p2:
            return JsonResponse({"ok": False, "error": "Passwords didn't match."}, status=400)
        need_email_otp = True

    # Send phone OTP
    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    # Stash session bundle
    request.session[CUSTOMER_SSR] = {
        "email": email,
        "existing": is_existing,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone_e164,
        "password1": p1,
        "need_email_otp": need_email_otp,
        "phone_verified": False,
        "email_verified": False,
    }
    request.session.modified = True

    return JsonResponse({"ok": True, "stage": "phone", "phone_e164": phone_e164})

@require_POST
def customer_begin_api(request):
    """
    NEW endpoint for the customer page's second step.
    Accepts:
      - existing user path: {email, phone?}
      - new user path: {email, first_name, last_name, phone, password1, password2}
    Decides which path based on whether User(email) exists.
    Sends SMS OTP and stashes a session bundle.
    """
    import json
    data = json.loads(request.body.decode() or "{}")

    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    user = User.objects.filter(email=email).first()
    is_existing = bool(user)

    # Decide phone source:
    phone_raw = (data.get("phone") or "").strip()
    if is_existing and not phone_raw:
        phone_raw = _get_verified_phone_for_user(user) or ""

    if not phone_raw:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

    # Normalize phone
    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    # Validate fields for NEW users
    first_name = (data.get("first_name") or "").strip()
    last_name  = (data.get("last_name") or "").strip()
    p1 = data.get("password1") or ""
    p2 = data.get("password2") or ""

    need_email_otp = False
    if not is_existing:
        # For new users we need first/last/passwords
        if not (first_name and last_name and p1 and p2):
            return JsonResponse({"ok": False, "error": "Please fill all fields."}, status=400)
        if p1 != p2:
            return JsonResponse({"ok": False, "error": "Passwords didn't match."}, status=400)
        need_email_otp = True

    # Send phone OTP
    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    # Stash session bundle
    request.session[CUSTOMER_SSR] = {
        "email": email,
        "existing": is_existing,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone_e164,
        "password1": p1,
        "need_email_otp": need_email_otp,
        "phone_verified": False,
        "email_verified": False,
    }
    request.session.modified = True

    return JsonResponse({"ok": True, "stage": "phone", "phone_e164": phone_e164})


@require_POST
def verify_otp(request):
    """
    Replaced: verify the CUSTOMER phone OTP using the session bundle.
    If user already exists -> finish here (no email OTP), attach/update CustomerProfile, activate user.
    If new user          -> send email OTP and return stage='email'.
    """
    import json
    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()

    ss = request.session.get(CUSTOMER_SSR)
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Restart sign up."}, status=400)

    phone_e164 = ss.get("phone")
    if not code or not phone_e164:
        return JsonResponse({"ok": False, "error": "Missing code or phone."}, status=400)

    # Verify SMS
    try:
        status = check_sms_otp(phone_e164, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    ss["phone_verified"] = True
    request.session[CUSTOMER_SSR] = ss
    request.session.modified = True

    # Existing user path → finish now (skip email OTP)
    if ss["existing"] and not ss["need_email_otp"]:
        user = User.objects.filter(email=ss["email"]).first()
        if not user:
            return JsonResponse({"ok": False, "error": "Account not found."}, status=400)

        # Ensure CustomerProfile + flags
        cp, _ = CustomerProfile.objects.get_or_create(user=user)
        cp.phone = phone_e164
        if hasattr(cp, "phone_verified"):
            cp.phone_verified = True
        if hasattr(cp, "email_verified"):
            cp.email_verified = True  # we consider email verified for existing user
        cp.save()

        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

        # Optionally log them in right now
        # login(request, user)

        # Clear or keep session as you prefer
        request.session.pop(CUSTOMER_SSR, None)
        request.session.modified = True

        return JsonResponse({"ok": True, "redirect": "/profile"})

    # New user path → send email OTP
    try:
        send_email_otp(ss["email"])
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Could not send email code: {e}"}, status=500)

    return JsonResponse({
        "ok": True,
        "stage": "email",
        "email": ss["email"],
        "message": "Phone verified. We sent a 6-digit code to your email."
    })


@require_POST
def verify_email_otp(request):
    """
    Replaced: complete CUSTOMER signup for NEW users only.
    Consumes session bundle and creates the Django user + CustomerProfile.
    """
    import json
    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()

    ss = request.session.get(CUSTOMER_SSR)
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Restart sign up."}, status=400)

    if not ss.get("need_email_otp"):
        return JsonResponse({"ok": False, "error": "Email OTP not required for this flow."}, status=400)

    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    # Verify email OTP
    try:
        status = check_email_otp(ss["email"], code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # Create final user
    user = User.objects.filter(email=ss["email"]).first()
    if user:
        # Edge: someone created it in the meantime. Keep their password.
        pass
    else:
        user = User.objects.create_user(
            username=ss["email"],
            email=ss["email"],
            password=ss["password1"],
            first_name=ss["first_name"],
            last_name=ss["last_name"],
        )
    user.is_active = True
    user.save(update_fields=["is_active"])

    # Create customer profile
    cp, _ = CustomerProfile.objects.get_or_create(user=user)
    cp.phone = ss["phone"]
    if hasattr(cp, "phone_verified"):
        cp.phone_verified = True
    if hasattr(cp, "email_verified"):
        cp.email_verified = True
    cp.save()

    # Cleanup
    request.session.pop(CUSTOMER_SSR, None)
    request.session.modified = True

    return JsonResponse({"ok": True, "message": "Verified. Please sign in.", "redirect": "/profile"})


def oauth_phone_page(request):
	print("DEBUG session keys:", list(request.session.keys()))
	if "pending_sociallogin" not in request.session:
		return HttpResponseBadRequest("No pending social signup. Start with Google.")
	email = request.session.get("pending_email", "")
	return render(request, "core/oauth_phone.html", {"email": email})

@require_POST
def oauth_phone_init(request):
    """
    POST {phone} (JSON or form) -> send OTP via Twilio Verify.
    Stores normalized phone in session for the next step.
    """
    if "pending_sociallogin" not in request.session:
        return JsonResponse({"ok": False, "error": "No pending social signup."}, status=400)

    # Accept JSON or form-POST
    raw = ""
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
        raw = (payload.get("phone") or "").strip()
    except Exception:
        pass
    if not raw:
        raw = (request.POST.get("phone") or "").strip()

    if not raw:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

    try:
        phone_e164 = to_e164_us(raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    request.session["pending_phone"] = phone_e164
    request.session.modified = True
    return JsonResponse({"ok": True, "phone_e164": phone_e164})

@require_POST
def oauth_phone_verify(request):
    sess = request.session
    if "pending_sociallogin" not in sess or "pending_phone" not in sess:
        return JsonResponse({"ok": False, "error": "Session expired. Restart Google sign-up."}, status=400)

    # 1) Read input
    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()
    phone_e164 = sess["pending_phone"]
    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    # 2) Verify phone via Twilio
    try:
        status = check_sms_otp(phone_e164, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # 3) Restore the pending SocialLogin
    try:
        sociallogin = SocialLogin.deserialize(sess["pending_sociallogin"])
    except Exception:
        return JsonResponse({"ok": False, "error": "Could not restore pending login."}, status=400)

    # 4) Ensure we have a *saved* Django user, then attach the social account
    email = (sess.get("pending_email") or sociallogin.user.email or "").lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Missing email from Google."}, status=400)

    user = User.objects.filter(email=email).first()
    if not user:
        # create a minimal saved user; we activate after phone verified
        user = User.objects.create_user(username=email, email=email)
        user.set_unusable_password()
        user.is_active = True  # phone verified => allow login
        user.save(update_fields=["is_active"])

    # Link this Google account to the user (works for new or existing users)
    sociallogin.connect(request, user)  # creates/updates SocialAccount & token

    # 5) Create/update the profile
    profile, _ = CustomerProfile.objects.get_or_create(user=user)
    profile.phone = phone_e164
    if hasattr(profile, "phone_verified"):
        profile.phone_verified = True
    if hasattr(profile, "email_verified"):
        profile.email_verified = True
    profile.save()

    # 6) Log the user in and clean session
    perform_login(request, user, email_verification="none")

    for k in ("pending_sociallogin", "pending_phone", "pending_email"):
        sess.pop(k, None)
    sess.modified = True

    return JsonResponse({"ok": True, "redirect": "/profile"})


# core/views.py
import json, random
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.db import transaction
from django.utils import timezone
from allauth.socialaccount.models import SocialLogin
from allauth.account.utils import perform_login

from .models import OwnerProfile, RestaurantProfile, CustomerProfile
from .utils import (
    send_sms_otp, check_sms_otp,
    send_email_otp, check_email_otp,
    to_e164_us
)

# -------------------------
# OWNER STANDARD SIGN-UP
# -------------------------
@require_POST
def owner_precheck_api(request):
    import json
    data = json.loads(request.body.decode() or "{}")
    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    # Block if a CustomerProfile already exists
    if OwnerProfile.objects.filter(user__email=email).exists():
        messages.error(request, "You already have an owner account.  Please sign in.")
        return JsonResponse({
            "ok":False,
            "redirect":reverse("core:restaurant_signin")
            },status = 409)

    user = User.objects.filter(email=email).first()
    has_verified_phone = bool(user and _get_verified_phone_for_user(user))
    return JsonResponse({
        "ok": True,
        "exists": bool(user),
        "has_verified_phone": has_verified_phone
    })

def owner_signup(request):
    if request.method != "POST":
        return render(request, "core/owner_signup.html")

    data = request.POST
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    email = data.get("email", "").lower().strip()
    username = data.get("username", "").strip()
    phone_raw = data.get("phone", "").strip()
    p1, p2 = data.get("p1"), data.get("p2")

    if not (first_name and last_name and email and username and phone_raw):
        return JsonResponse({"ok": False, "error": "All fields are required"}, status=400)
    if p1 != p2:
        return JsonResponse({"ok": False, "error": "Passwords did not match"}, status=400)

    try:
        phone = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid phone"}, status=400)

    # create inactive user
    user = User.objects.create_user(username=username, email=email, password=p1,
                                    first_name=first_name, last_name=last_name)
    user.is_active = False
    user.save()

    # stash owner profile (inactive until verified)
    OwnerProfile.objects.create(user=user, phone=phone)

    # send phone OTP
    send_sms_otp(phone)

    # store phone in session for verification step
    request.session["pending_owner_phone"] = phone
    request.session["pending_owner_email"] = email

    return JsonResponse({"ok": True, "stage": "phone", "phone": phone})

from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest
from django.contrib.auth.models import User
from .models import OwnerProfile
from .utils import to_e164_us, send_sms_otp, check_sms_otp, send_email_otp, check_email_otp
import json

# -------------------------
# OWNER EMAIL-FIRST FLOW (replaces your existing owner_* APIs)
# -------------------------
OWNER_SSR_KEY = "owner_signup"

@require_POST
def owner_signup_api(request):
    """
    POST JSON (one endpoint for both paths):
      - existing user path: { email, phone? }
      - new user path:      { email, first_name, last_name, phone, password1, password2 }
    Behavior:
      - If User exists: do NOT change password; skip email OTP; SMS only.
      - If no User: require first/last/password; do SMS then Email OTP.
      - Prevent if an OwnerProfile already exists for this email.
    """
    import json
    data = json.loads(request.body.decode() or "{}")

    email = (data.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Email is required."}, status=400)

    if OwnerProfile.objects.filter(user__email=email).exists():
        return JsonResponse({"ok": False, "error": "An owner account already exists for this email."}, status=400)

    user = User.objects.filter(email=email).first()
    is_existing = bool(user)

    # Choose phone
    phone_raw = (data.get("phone") or "").strip()
    if is_existing and not phone_raw:
        phone_raw = _get_verified_phone_for_user(user) or ""

    if not phone_raw:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    # New user requirements
    first_name = (data.get("first_name") or "").strip()
    last_name  = (data.get("last_name") or "").strip()
    p1 = data.get("password1") or ""
    p2 = data.get("password2") or ""
    need_email_otp = False

    if not is_existing:
        if not (first_name and last_name and p1 and p2):
            return JsonResponse({"ok": False, "error": "Please fill all fields."}, status=400)
        if p1 != p2:
            return JsonResponse({"ok": False, "error": "Passwords didn't match."}, status=400)
        need_email_otp = True

    # Send SMS OTP
    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    # Stash session
    request.session[OWNER_SSR_KEY] = {
        "email": email,
        "existing": is_existing,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone_e164,
        "password1": p1,
        "need_email_otp": need_email_otp,
        "phone_verified": False,
        "email_verified": False,
    }
    request.session.modified = True

    return JsonResponse({"ok": True, "stage": "phone", "phone_e164": phone_e164})


@require_POST
def owner_verify_phone_api(request):
    """
    After owner SMS code:
      - If existing user: create OwnerProfile now and redirect to /restaurant/onboard
      - If new user: send email OTP then wait for owner_verify_email_api
    """
    import json
    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()

    ss = request.session.get(OWNER_SSR_KEY)
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Start again."}, status=400)

    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    phone_e164 = ss["phone"]
    try:
        status = check_sms_otp(phone_e164, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    ss["phone_verified"] = True
    request.session[OWNER_SSR_KEY] = ss
    request.session.modified = True

    # Existing user path → create OwnerProfile now
    if ss["existing"] and not ss["need_email_otp"]:
        user = User.objects.filter(email=ss["email"]).first()
        if not user:
            return JsonResponse({"ok": False, "error": "Account not found."}, status=400)

        op, _ = OwnerProfile.objects.get_or_create(user=user)
        op.phone = phone_e164
        if hasattr(op, "phone_verified"):
            op.phone_verified = True
        if hasattr(op, "email_verified"):
            op.email_verified = True
        op.save()

        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

        # Clean up session if you like
        request.session.pop(OWNER_SSR_KEY, None)
        request.session.modified = True

        return JsonResponse({"ok": True, "redirect": "/restaurant/onboard"})

    # New user path → email OTP next
    try:
        send_email_otp(ss["email"])
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Could not send email code: {e}"}, status=500)

    return JsonResponse({"ok": True, "stage": "email", "email": ss["email"]})


@require_POST
def owner_verify_email_api(request):
    """
    Complete owner signup for NEW users only.
    Creates Django User + OwnerProfile; then redirect to restaurant onboarding.
    """
    import json
    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()

    ss = request.session.get(OWNER_SSR_KEY)
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Start again."}, status=400)

    if not ss.get("need_email_otp"):
        return JsonResponse({"ok": False, "error": "Email OTP not required."}, status=400)

    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    try:
        status = check_email_otp(ss["email"], code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # Create user
    user = User.objects.filter(email=ss["email"]).first()
    if not user:
        user = User.objects.create_user(
            username=ss["email"],
            email=ss["email"],
            password=ss["password1"],
            first_name=ss["first_name"],
            last_name=ss["last_name"],
        )
    user.is_active = True
    user.save(update_fields=["is_active"])

    # Owner profile
    op, _ = OwnerProfile.objects.get_or_create(user=user)
    op.phone = ss["phone"]
    if hasattr(op, "phone_verified"):
        op.phone_verified = True
    if hasattr(op, "email_verified"):
        op.email_verified = True
    op.save()

    # Clear and move to onboard
    request.session.pop(OWNER_SSR_KEY, None)
    request.session.modified = True

    return JsonResponse({"ok": True, "redirect": "/restaurant/onboard"})

# --- Owner Existing-User flow: send OTP, then verify ---
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.contrib.auth.models import User
from django.contrib.auth import login
from django.utils import timezone

from .models import CustomerProfile, OwnerProfile
from .utils import to_e164_us, send_sms_otp, check_sms_otp

OWNER_EXISTING_KEY = "owner_existing_begin"

@require_POST
def owner_begin_existing_api(request):
    """
    Body: { email, phone? }
    If user exists:
      - if they already have a verified phone on any profile, use it;
      - else normalize provided phone.
      Send SMS OTP and stash {email, phone_e164} in session.
    """
    data = json.loads(request.body.decode() or "{}")
    email = (data.get("email") or "").strip().lower()
    phone_raw = (data.get("phone") or "").strip() or None
    if not email:
        return JsonResponse({"ok": False, "error": "Email required."}, status=400)

    user = User.objects.filter(email=email).first()
    if not user:
        return JsonResponse({"ok": False, "error": "No user for that email."}, status=400)

    # try to reuse a verified phone if you track those flags
    phone_e164 = None
    cp = CustomerProfile.objects.filter(user=user).first()
    if cp and getattr(cp, "phone_verified", False) and cp.phone:
        phone_e164 = cp.phone
    else:
        op = OwnerProfile.objects.filter(user=user).first()
        if op and getattr(op, "phone_verified", False) and op.phone:
            phone_e164 = op.phone

    if not phone_e164:
        if not phone_raw:
            return JsonResponse({"ok": False, "error": "Phone required."}, status=400)
        try:
            phone_e164 = to_e164_us(phone_raw)
        except Exception:
            return JsonResponse({"ok": False, "error": "Enter a valid US phone."}, status=400)

    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    request.session[OWNER_EXISTING_KEY] = {"email": email, "phone": phone_e164}
    request.session.modified = True
    return JsonResponse({"ok": True, "phone_e164": phone_e164})


@require_POST
def owner_existing_verify_phone_api(request):
    """
    Body: { code }
    Verify SMS; on success create (or update) OwnerProfile, log user in, redirect to onboarding.
    """
    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()
    ss = request.session.get(OWNER_EXISTING_KEY)
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Start again."}, status=400)
    if not code:
        return JsonResponse({"ok": False, "error": "Code required."}, status=400)

    email = ss["email"]; phone = ss["phone"]
    try:
        status = check_sms_otp(phone, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verify error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    user = User.objects.filter(email=email).first()
    if not user:
        return JsonResponse({"ok": False, "error": "User not found."}, status=400)

    # ensure owner profile
    op, _ = OwnerProfile.objects.get_or_create(user=user)
    op.phone = phone
    if hasattr(op, "phone_verified"):
        op.phone_verified = True
    if hasattr(op, "email_verified"):
        op.email_verified = True  # since the email already belongs to this user
    op.save()

    login(request, user)
    request.session.pop(OWNER_EXISTING_KEY, None)
    request.session.modified = True
    return JsonResponse({"ok": True, "redirect": "/restaurant/onboard"})

# helper
def set_current_restaurant(request, restaurant_id: int):
    request.session["current_restaurant_id"] = restaurant_id
    request.session.modified = True

def get_current_restaurant(request):
    rid = request.session.get("current_restaurant_id")
    if not rid:
        return None
    from .models import RestaurantProfile
    return RestaurantProfile.objects.filter(id=rid).first()


@require_POST
@transaction.atomic
def owner_restaurant_save_api(request):
    """
    Body: {legal_name, dba_name, phone, address}
    Now we finally CREATE the User + RestaurantProfile + OwnerProfile.
    """
    ss = request.session.get("owner_signup")
    if not (ss and ss.get("phone_verified") and ss.get("email_verified")):
        return JsonResponse({"ok": False, "error": "Complete verification first."}, status=400)

    data = json.loads(request.body.decode() or "{}")
    legal_name = (data.get("legal_name") or "").strip()
    dba_name   = (data.get("dba_name") or "").strip()
    phone      = (data.get("phone") or "").strip()
    address    = (data.get("address") or "").strip()

    if not legal_name:
        return JsonResponse({"ok": False, "error": "Legal name is required."}, status=400)

    # Create final user
    if User.objects.filter(username=ss["username"]).exists() or User.objects.filter(email=ss["email"]).exists():
        return JsonResponse({"ok": False, "error": "This account already exists."}, status=400)

    user = User.objects.create_user(
        username=ss["username"],
        email=ss["email"],
        password=ss["password1"],
        first_name=ss["first_name"],
        last_name=ss["last_name"],
        is_active=True
    )

    # Create restaurant
    RestaurantProfile.objects.create(
        user=user,
        legal_name=legal_name,
        email=ss["email"],
        dba_name=dba_name,
        phone=phone or ss["phone"],
        address=address,
        processor="stripe",
        processor_verification="pending",
        payout_status="pending",
        is_active=False,
    )

    # Create owner profile AFTER both OTPs + restaurant
    OwnerProfile.objects.create(
        user=user,
        phone=ss["phone"],
        phone_verified=True,
        email_verified=True
    )

    # Clear the session bundle
    request.session.pop("owner_signup", None)
    request.session.modified = True

    return JsonResponse({"ok": True, "redirect": "/owner/dashboard"})

# -------------------------
# OWNER GOOGLE OAuth
# -------------------------

def oauth_owner_phone_page(request):
    """
    Page shown right after Google for OWNERS. Requires pending_sociallogin in session.
    """
    if "pending_sociallogin" not in request.session:
        return HttpResponseBadRequest("No pending owner signup. Start with Google.")
    if request.session.get("auth_role") != "owner":
        return HttpResponseBadRequest("Wrong flow.")

    email = request.session.get("pending_email", "")
    return render(request, "core/oauth_owner_phone.html", {"email": email})


@require_POST
def oauth_owner_phone_init(request):
    """
    POST { phone } -> send OTP for owner flow. Stores normalized phone in session.
    """
    sess = request.session
    if "pending_sociallogin" not in sess or sess.get("auth_role") != "owner":
        return JsonResponse({"ok": False, "error": "No pending owner signup."}, status=400)

    # JSON or form
    raw = ""
    try:
        raw = (json.loads((request.body or b"").decode() or "{}").get("phone") or "").strip()
    except Exception:
        pass
    if not raw:
        raw = (request.POST.get("phone") or "").strip()

    if not raw:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

    try:
        phone_e164 = to_e164_us(raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    sess["pending_owner_phone"] = phone_e164
    sess.modified = True
    return JsonResponse({"ok": True, "phone_e164": phone_e164})


@require_POST
def oauth_owner_phone_verify(request):
    """
    POST { code } -> verify OTP, attach Google account, create OwnerProfile, log in,
    then send owner to restaurant onboarding.
    """
    sess = request.session
    if "pending_sociallogin" not in sess or sess.get("auth_role") != "owner":
        return JsonResponse({"ok": False, "error": "Session expired. Restart Google sign-up."}, status=400)
    if "pending_owner_phone" not in sess:
        return JsonResponse({"ok": False, "error": "No phone on file. Send code first."}, status=400)

    data = json.loads(request.body.decode() or "{}")
    code = (data.get("code") or "").strip()
    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    phone_e164 = sess["pending_owner_phone"]

    # Verify with Twilio
    try:
        status = check_sms_otp(phone_e164, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # Restore Google login
    try:
        sociallogin = SocialLogin.deserialize(sess["pending_sociallogin"])
    except Exception:
        return JsonResponse({"ok": False, "error": "Could not restore pending login."}, status=400)

    email = (sess.get("pending_email") or sociallogin.user.email or "").lower()
    if not email:
        return JsonResponse({"ok": False, "error": "Missing email from Google."}, status=400)

    # Ensure saved user
    user = User.objects.filter(email=email).first()
    if not user:
        user = User.objects.create_user(username=email, email=email)
        user.set_unusable_password()
        user.is_active = True
        user.save(update_fields=["is_active"])

    # Link Google account to this user
    sociallogin.connect(request, user)

    # Ensure OwnerProfile with verified phone
    owner, _ = OwnerProfile.objects.get_or_create(user=user)
    owner.phone = phone_e164
    if hasattr(owner, "phone_verified"):
        owner.phone_verified = True
    if hasattr(owner, "email_verified"):
        owner.email_verified = True
    owner.save()

    # Log in and clean up
    perform_login(request, user, email_verification="none")
    for k in ("pending_sociallogin", "pending_email", "pending_owner_phone", "auth_role"):
        sess.pop(k, None)
    sess.modified = True

    # Send to owner onboarding to create the RestaurantProfile
    return JsonResponse({"ok": True, "redirect": "/restaurant/onboard"})

@login_required
def post_login_owner(request):
    op = getattr(request.user, "owner_profile", None)
    if not op:
        # no owner profile yet -> start owner onboarding
        return redirect("core:owner_signup")  # or wherever your owner flow begins

    # all restaurants this owner can access
    qs = RestaurantProfile.objects.filter(owners__owner=op, owners__is_active=True).order_by("created_at")

    if not qs.exists():
         # owner profile exists but no restaurants yet -> onboard first restaurant
        return redirect("core:restaurant_onboard")

    # ensure a current restaurant is set in session
    current = get_current_restaurant(request)
    if not current or not qs.filter(id=current.id).exists():
        set_current_restaurant(request, qs.first().id)

    return redirect("core:owner_dashboard")


# core/views.py (add near your other imports)
from datetime import timedelta
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.core.mail import send_mail
from django.conf import settings

from .models import ManagerInvite  # assumes you already have this model

# views.py
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import json

from .models import RestaurantProfile, OwnerProfile, Ownership, ManagerInvite

def _current_restaurant(request):
    """Resolve the restaurant for the signed-in owner."""
    # 1) session selection
    rid = request.session.get("current_restaurant_id")
    if rid:
        rp = RestaurantProfile.objects.filter(id=rid).first()
        if rp:
            return rp

    # 2) first active restaurant this owner owns
    op = OwnerProfile.objects.filter(user=request.user).first()
    if not op:
        return None

    # Use through model; respect is_active if present
    ow_qs = Ownership.objects.filter(owner=op)
    if any(f.name == "is_active" for f in Ownership._meta.fields):
        ow_qs = ow_qs.filter(is_active=True)

    rid = ow_qs.values_list("restaurant_id", flat=True).first()
    if rid:
        rp = RestaurantProfile.objects.filter(id=rid).first()
        if rp:
            # remember it in session for next time
            request.session["current_restaurant_id"] = rp.id
            request.session.modified = True
            return rp
    return None

@login_required
@require_http_methods(["GET", "POST"])
def owner_invite_manager(request):
    """Owner sends an invite for the *current* restaurant."""
    rp = _current_restaurant(request)
    if request.method == "GET":
        # If you prefer to block here, you can render a page explaining they must onboard first.
        if not rp:
            return JsonResponse({"ok": False, "error": "Create your restaurant profile first."}, status=400)
        return render(request, "core/owner_invite_manager.html", {"restaurant": rp})

    # POST (JSON or form)
    if not rp:
        return JsonResponse({"ok": False, "error": "Create your restaurant profile first."}, status=400)

    email = ""
    expires_minutes = 120

    # JSON body first
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
        email = (payload.get("email") or "").strip().lower()
        if payload.get("expires_minutes") is not None:
            expires_minutes = int(payload["expires_minutes"])
    except Exception:
        pass

    # Fallback to form POST
    if not email:
        email = (request.POST.get("email") or "").strip().lower()
    if request.POST.get("expires_minutes"):
        try:
            expires_minutes = int(request.POST.get("expires_minutes"))
        except ValueError:
            pass

    if not email:
        return JsonResponse({"ok": False, "error": "Please provide an email."}, status=400)

    invite = ManagerInvite.objects.create(
        restaurant=rp,
        email=email,
        expires_at=timezone.now() + timedelta(minutes=expires_minutes),
    )

    link = f"{request.scheme}://{request.get_host()}/manager/accept?token={invite.token}"
    rest_name = rp.dba_name or rp.legal_name or "your restaurant"

    try:
        send_manager_invite_email(
            to_email=email,
            invite_link=link,
            restaurant_name=rest_name,
            expires_at=invite.expires_at,
        )
        email_ok = True
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Email send failed: {e}"}, status=500)

    return JsonResponse({
        "ok": True,
        "message": f"Invite sent to {email}.",
        "invite": {
            "email": email,
            "token": str(invite.token),
            "expires_at": invite.expires_at.isoformat(),
            "link": link,
            "email_sent": email_ok,
        },
    })



def _invite_is_valid(invite) -> bool:
    """Safe validity check even if your model doesn't have `is_valid` property."""
    if invite is None:
        return False
    # already accepted?
    if getattr(invite, "accepted_at", None):
        return False
    # expired?
    expires_at = getattr(invite, "expires_at", None)
    if expires_at and timezone.now() > expires_at:
        return False
    return True

from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth import login, get_user_model
from django.utils import timezone
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse

import json

User = get_user_model()

@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def manager_accept_invite(request):
    """
    Manager invite accept flow.

    - GET: render accept page for a valid invite.
    - POST action=init:
        * If user with invite.email EXISTS: ignore password fields, just send SMS OTP,
          stash session with flag existing_user=True.
        * If user does NOT exist: validate passwords, send SMS OTP, stash session.
    - POST action=verify:
        * Check OTP. If existing_user:
            - attach/update ManagerProfile for the existing user
            - mark invite accepted
            - login and redirect to dashboard
          else:
            - create user, attach ManagerProfile
            - mark invite accepted, login, redirect
    """
    PENDING_KEY = "pending_manager_accept"

    # --- GET ---
    if request.method == "GET":
        token = request.GET.get("token")
        invite = None
        if token:
            invite = ManagerInvite.objects.filter(token=token).first()
        if not invite or not getattr(invite, "is_valid", False):
            return render(request, "core/manager_accept_invalid.html")

        # Optional: tell template whether the user already exists
        existing_user = User.objects.filter(email__iexact=invite.email).exists()
        return render(
            request,
            "core/manager_accept.html",
            {"invite": invite, "existing_user": existing_user},
        )

    # --- POST JSON ---
    try:
        data = json.loads((request.body or b"").decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "Bad JSON."}, status=400)

    action = (data.get("action") or "").strip()
    token = (data.get("token") or "").strip()
    invite = ManagerInvite.objects.filter(token=token).first()
    if not invite or not getattr(invite, "is_valid", False):
        return JsonResponse({"ok": False, "error": "Invite is invalid or expired."}, status=400)

    # Common bits
    email = (data.get("email") or "").strip().lower()
    if email != invite.email.lower():
        return JsonResponse({"ok": False, "error": "Email must match the invited address."}, status=400)

    if action == "init":
        # Determine whether the invited email already maps to a User
        user = User.objects.filter(email__iexact=email).first()
        existing_user = bool(user)

        # For both paths we require a phone to OTP
        phone_raw = (data.get("phone") or "").strip()
        if not phone_raw:
            return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

        try:
            phone_e164 = to_e164_us(phone_raw)
        except Exception:
            return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

        # If you want to keep phone uniqueness for managers, keep this:
        if ManagerProfile.objects.filter(phone=phone_e164).exists():
            return JsonResponse({"ok": False, "error": "Phone already in use."}, status=400)

        # Only enforce/collect password for new users
        password1 = data.get("password1") or ""
        password2 = data.get("password2") or ""
        if not existing_user:
            if not password1 or password1 != password2:
                return JsonResponse({"ok": False, "error": "Passwords didn't match."}, status=400)

        # Send OTP
        try:
            send_sms_otp(phone_e164)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

        # Stash pending info
        request.session[PENDING_KEY] = {
            "token": str(invite.token),
            "email": email,
            "phone": phone_e164,
            "existing_user": existing_user,
            # keep the password only if we're creating a new user
            "password": password1 if not existing_user else None,
        }
        request.session.modified = True
        return JsonResponse({"ok": True, "stage": "code", "phone_e164": phone_e164})

    if action == "verify":
        code = (data.get("code") or "").strip()
        pending = request.session.get(PENDING_KEY) or {}
        if not pending or str(invite.token) != pending.get("token"):
            return JsonResponse({"ok": False, "error": "Session expired. Restart from invite link."}, status=400)

        phone_e164 = pending.get("phone")
        if not code or not phone_e164:
            return JsonResponse({"ok": False, "error": "Missing code."}, status=400)

        # Verify code with Twilio Verify
        try:
            status = check_sms_otp(phone_e164, code)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
        if status != "approved":
            return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

        # Branch: existing user vs new user
        email = pending["email"]
        existing_user = bool(pending.get("existing_user"))

        if existing_user:
            user = User.objects.filter(email__iexact=email).first()
            if not user:
                # Safety (shouldn't happen)
                return JsonResponse({"ok": False, "error": "Account disappeared. Try again."}, status=400)
        else:
            # Create and activate user (username=email)
            password = pending.get("password") or _generate_code(10)  # fallback; shouldn't happen
            user = User.objects.create_user(username=email, email=email, password=password)
            user.is_active = True
            user.save(update_fields=["is_active"])

        # Create/attach ManagerProfile
        mp, created = ManagerProfile.objects.get_or_create(user=user)
        mp.phone = phone_e164
        if hasattr(mp, "phone_verified"):
            mp.phone_verified = True
        if hasattr(mp, "email_verified"):
            # possession of the invite link implies email proof
            mp.email_verified = True
        # attach the restaurant from the invite if not set
        if not getattr(mp, "restaurant_id", None):
            mp.restaurant = invite.restaurant
        mp.save()

        # Mark invite accepted
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])

        # Clean session + log in + redirect
        try:
            del request.session[PENDING_KEY]
        except KeyError:
            pass
        login(request, user)

        return JsonResponse({"ok": True, "redirect": reverse("core:manager_dashboard")})

    return JsonResponse({"ok": False, "error": "Unknown action."}, status=400)

    if action == "verify":
        code = (data.get("code") or "").strip()
        pending = request.session.get(PENDING_KEY) or {}
        if not pending or str(invite.token) != pending.get("token"):
            return JsonResponse({"ok": False, "error": "Session expired. Restart from invite link."}, status=400)

        phone_e164 = pending.get("phone")
        if not code or not phone_e164:
            return JsonResponse({"ok": False, "error": "Missing code."}, status=400)

        # Verify code with Twilio Verify
        try:
            status = check_sms_otp(phone_e164, code)
        except Exception as e:
            return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
        if status != "approved":
            return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

        # Create/activate the user
        email = pending["email"]
        password = pending["password"]
        user = User.objects.filter(username=email).first()
        if user:
            user.email = email
            user.set_password(password)
            user.is_active = True
            user.save()
        else:
            user = User.objects.create_user(username=email, email=email, password=password)
            user.is_active = True
            user.save()

        # Create/attach ManagerProfile (phone verified)
        mp, created = ManagerProfile.objects.get_or_create(user=user, defaults={
            "phone": phone_e164,
            "phone_verified": True,
            "email_verified": True,  # invite email is verified by link possession
            "restaurant": invite.restaurant,
        })
        if not created:
            # Update fields if needed
            mp.phone = phone_e164
            mp.phone_verified = True
            mp.email_verified = True
            if not mp.restaurant:
                mp.restaurant = invite.restaurant
            mp.save()

        # Mark invite accepted
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])

        # Clean session + log in
        request.session.pop(PENDING_KEY, None)
        login(request, user)

        return JsonResponse({"ok": True, "redirect": "/manager/dashboard/"})

    return JsonResponse({"ok": False, "error": "Unknown action."}, status=400)

@login_required
def manager_dashboard(request):
    """
    Very simple manager dashboard.
    Only managers (users with a ManagerProfile) can see it.
    """
    mp = getattr(request.user, "managerprofile", None)
    if not mp:
        # Not a manager (or not linked yet) -> send to the manager sign-in tab
        return redirect("/restaurant/signin?tab=manager")

    rp = getattr(mp, "restaurant", None)
    return render(request, "core/manager_dashboard.html", {
        "mp": mp,
        "restaurant": rp,
    })
# -------------------------
# PAGES
# -------------------------

def profile(request):
    return render(request, "core/profile.html")

from django.contrib.auth import authenticate, login
from django.http import JsonResponse
from django.urls import reverse

def restaurant_signin(request):
    if request.method == "GET":
        active_tab = request.GET.get("tab", "owner")
        return render(request, "core/restaurant_signin.html", {"active_tab": active_tab})

    portal = (request.POST.get("portal") or "owner").strip()
    email  = (request.POST.get("email") or "").strip().lower()
    pwd    = request.POST.get("password") or ""

    if not email or not pwd:
        return JsonResponse({"ok": False, "error": "Email and password are required."}, status=400)

    user = authenticate(request, username=email, password=pwd)
    if not user:
        return JsonResponse({"ok": False, "error": "Invalid email or password."}, status=400)

    login(request, user)

    if portal == "manager":
        return JsonResponse({"ok": True, "redirect": reverse("core:manager_dashboard")})

    # Owner flow
    owner, _ = OwnerProfile.objects.get_or_create(user=user)

    # legacy one-to-one support
    legacy_rp = getattr(user, "restaurant_profile", None)
    if legacy_rp:
        dest = reverse("core:owner_dashboard") if getattr(legacy_rp, "is_active", False) else reverse("core:restaurant_onboard")
        return JsonResponse({"ok": True, "redirect": dest})

    # multi-restaurant: any restaurants owned?
    has_any_restaurant = False
    first_rp = None
    if hasattr(RestaurantProfile, "owners"):
        qs = RestaurantProfile.objects.filter(owners__user=user).order_by("id")
        has_any_restaurant = qs.exists()
        first_rp = qs.first()

    if has_any_restaurant and first_rp:
        request.session["current_restaurant_id"] = first_rp.id
        request.session.modified = True
        return JsonResponse({"ok": True, "redirect": reverse("core:owner_dashboard")})

    return JsonResponse({"ok": True, "redirect": reverse("core:restaurant_onboard")})


# core/views.py
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse

def _get_owner_profile(user):
    from .models import OwnerProfile
    return OwnerProfile.objects.filter(user=user).first()

def _restaurants_for_owner(user, owner_profile):
    """
    Robustly fetch restaurants for an owner:
      - NEW schema: RestaurantProfile <-> Ownership <-> OwnerProfile
      - LEGACY schema: RestaurantProfile has FK 'user'
    """
    from .models import RestaurantProfile

    # NEW: use the through model directly to avoid related_name mismatches
    if hasattr(RestaurantProfile, "owners"):
        try:
            from .models import Ownership
            ow_qs = Ownership.objects.filter(owner=owner_profile)
            # Respect is_active flag if present
            if any(f.name == "is_active" for f in Ownership._meta.fields):
                ow_qs = ow_qs.filter(is_active=True)
            rest_ids = ow_qs.values_list("restaurant_id", flat=True)
            qs = RestaurantProfile.objects.filter(id__in=rest_ids)
        except Exception:
            # Fallback: plain M2M without through extras
            qs = RestaurantProfile.objects.filter(owners=owner_profile)
    # LEGACY: one restaurant per user via FK
    elif hasattr(RestaurantProfile, "user"):
        # If your legacy model has no is_active, remove that filter
        fields = {f.name for f in RestaurantProfile._meta.fields}
        flt = {"user": user}
        if "is_active" in fields:
            flt["is_active"] = True
        qs = RestaurantProfile.objects.filter(**flt)
    else:
        qs = RestaurantProfile.objects.none()

    # Order safely
    fields = {f.name for f in RestaurantProfile._meta.fields}
    return qs.order_by("created_at" if "created_at" in fields else "id")

def get_current_restaurant(request):
    from .models import RestaurantProfile
    rid = request.session.get("current_restaurant_id")
    return RestaurantProfile.objects.filter(id=rid).first() if rid else None

def set_current_restaurant(request, rid: int):
    request.session["current_restaurant_id"] = int(rid)
    request.session.modified = True

@login_required
def owner_dashboard(request):
    op = _get_owner_profile(request.user)
    if not op:
        # No OwnerProfile yet -> send to owner signup/onboarding
        return redirect(reverse("core:owner_signup"))

    restaurants = _restaurants_for_owner(request.user, op)
    current = get_current_restaurant(request) or restaurants.first()

    if current and not request.session.get("current_restaurant_id"):
        set_current_restaurant(request, current.id)

    return render(request, "core/owner_dashboard.html", {
        "restaurants": restaurants,
        "current": current,
        "profile": op,
    })



# core/views.py
from urllib.parse import quote
from django.shortcuts import redirect
from urllib.parse import quote

def owner_google_start(request):
    request.session["auth_role"] = "owner"  # keep this
    request.session.modified = True
    # also encode role in the next url so we don't depend on session surviving
    next_url = "/post-login-owner/?role=owner"
    return redirect(f"/accounts/google/login/?process=login&next={quote(next_url)}")


@login_required
def manager_dashboard(request):
    mp = getattr(request.user, "manager_profile", None)
    return render(request, "core/manager_dashboard.html", {"profile": mp})

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
import json

from .models import OwnerProfile, RestaurantProfile

# helpers.py (or keep near your view)

from .models import OwnerProfile, RestaurantProfile

def attach_owner_to_restaurant(owner: OwnerProfile, rp: RestaurantProfile):
    """
    Attach an owner to a restaurant in a schema-agnostic way:
      - If RestaurantProfile.owners is a ManyToManyField, create/get the through row.
      - Otherwise, assume there's a FK on OwnerProfile pointing to RestaurantProfile, and set it.
    """

    rel = getattr(RestaurantProfile, "owners", None)

    # CASE 1: True ManyToMany (has .through)
    if rel is not None and hasattr(rel, "through"):
        through = rel.through

        # find FK field names on the through model
        rp_fk_name = None
        owner_fk_name = None
        for f in through._meta.get_fields():
            remote = getattr(f, "remote_field", None)
            if not remote:
                continue
            if remote.model is RestaurantProfile:
                rp_fk_name = f.name
            elif remote.model is OwnerProfile:
                owner_fk_name = f.name

        if not rp_fk_name or not owner_fk_name:
            raise RuntimeError(
                "Ownership through-model must have FKs to RestaurantProfile and OwnerProfile."
            )

        lookup = {rp_fk_name: rp, owner_fk_name: owner}
        through.objects.get_or_create(**lookup)
        return

    # CASE 2: Reverse FK (no .through) → set FK on OwnerProfile
    # Find a FK on OwnerProfile that targets RestaurantProfile and set it.
    for f in OwnerProfile._meta.get_fields():
        remote = getattr(f, "remote_field", None)
        if remote and remote.model is RestaurantProfile:
            setattr(owner, f.name, rp)       # e.g. owner.restaurant = rp
            owner.save(update_fields=[f.name])
            return

    # If we got here, there is no linkable relation.
    raise RuntimeError(
        "Could not find a way to link OwnerProfile to RestaurantProfile. "
        "Add a ManyToManyField (owners) or a FK on OwnerProfile."
    )

def attach_owner_to_restaurant(rp, owner):
    """
    Link OwnerProfile <owner> to RestaurantProfile <rp> regardless of how the
    relation is modeled. Handles:
      A) rp.owners = ManyToManyField(OwnerProfile, through='Ownership')
      B) Ownership(owner=OwnerProfile, restaurant=RestaurantProfile) explicit
      C) rp has FK like rp.owner / rp.owner_profile
    """
    # A/B: many-to-many via through=Ownership
    if hasattr(rp.__class__, "owners"):
        through = rp.__class__.owners.through  # Ownership model
        # Its FK field names vary; detect them
        fks = {f.name: f for f in through._meta.fields if f.is_relation}
        # Find the FK names pointing to OwnerProfile and RestaurantProfile
        owner_fk = None
        rest_fk  = None
        for name, f in fks.items():
            if getattr(f.related_model, "__name__", "") == "OwnerProfile":
                owner_fk = name
            if getattr(f.related_model, "__name__", "") == "RestaurantProfile":
                rest_fk = name
        if not owner_fk or not rest_fk:
            raise RuntimeError("Ownership through model doesn't point to OwnerProfile/RestaurantProfile.")

        # Create-if-missing
        defaults = {}
        filter_kwargs = {owner_fk: owner, rest_fk: rp}
        through.objects.get_or_create(**filter_kwargs, defaults=defaults)
        return

    # C: simple FK on RestaurantProfile (e.g., rp.owner or rp.owner_profile)
    if hasattr(rp, "owner"):
        rp.owner = owner
        rp.save(update_fields=["owner"])
        return
    if hasattr(rp, "owner_profile"):
        rp.owner_profile = owner
        rp.save(update_fields=["owner_profile"])
        return

    # If none matched, we can't link
    raise RuntimeError("Could not find a way to link OwnerProfile to RestaurantProfile. "
                       "Add a ManyToManyField (owners) or a FK on OwnerProfile/RestaurantProfile.")

from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse
from django.urls import reverse
import json

@login_required
@require_http_methods(["GET", "POST"])
def restaurant_onboard(request):
    # Ensure there is an OwnerProfile for the current user
    owner, _ = OwnerProfile.objects.get_or_create(user=request.user)

    if request.method == "GET":
        # Prefill the form: try legacy single-restaurant record if present
        legacy_profile = getattr(request.user, "restaurant_profile", None)
        #…or, if you're already on multi-restaurant, you can choose the “current” one from session
        current_id = request.session.get("current_restaurant_id")
        current_profile = None
        if current_id:
            current_profile = RestaurantProfile.objects.filter(id=current_id).first()

        profile = current_profile or legacy_profile
        return render(request, "core/restaurant_onboard.html", {"owner": owner, "profile": profile})

    # ----- POST JSON -----
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)

    legal_name = (data.get("legal_name") or "").strip()
    email      = (data.get("email") or "").strip().lower()
    dba_name   = (data.get("dba_name") or "").strip()
    phone      = (data.get("phone") or "").strip()
    address    = (data.get("address") or "").strip()

    if not legal_name or not email:
        return JsonResponse({"ok": False, "error": "Legal name and email are required."}, status=400)

    # If you still have legacy 1:1 `RestaurantProfile.user`, keep using get_or_create(user=…)
    if hasattr(RestaurantProfile, "user"):
        rp, _ = RestaurantProfile.objects.get_or_create(user=request.user)
        rp.legal_name = legal_name
        rp.email      = email
        rp.dba_name   = dba_name
        rp.phone      = phone
        rp.address    = address
        rp.is_active  = True
        rp.save()
    else:
        # Pure multi-restaurant: always create a new restaurant record
        rp = RestaurantProfile.objects.create(
            legal_name=legal_name,
            dba_name=dba_name,
            email=email,
            phone=phone,
            address=address,
            is_active=True,
        )

    # Link ownership (works whether you have M2M through or FK)
    try:
        attach_owner_to_restaurant(rp, owner)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Ownership link failed: {e}"}, status=500)

    # remember selection
    request.session["current_restaurant_id"] = rp.id
    request.session.modified = True

    return JsonResponse({"ok": True, "redirect": reverse("core:owner_dashboard")})



def signin(request):

	if request.method == 'POST':
		username = request.POST.get('username')
		password1 = request.POST.get('password1')

		user = authenticate(username = username, password = password1)

		if user is not None:
			login(request, user)

			request.session.set_expiry(2592000)  # 2 weeks (in seconds)

			next_url = request.POST.get('next')
			if next_url:
				return HttpResponseRedirect(next_url)  # Redirect to the next URL

			return redirect('core:profile')  # Default redirection
		
		else:
			messages.error(request, "Invalid username or password.")
			return redirect('core:signin')	

	return render(request, "core/signin.html")

def signout(request):
	logout(request)
	return redirect('core:homepage')

def forgotPassEmail(request):
	if request.method == "POST":
		email = request.POST.get('email')

		if User.objects.filter(email=email).exists():
			myuser = User.objects.get(email = email)
			if myuser.is_active == False:
				messages.error(request,'Please Sign Up again.')
				return redirect('core:signup')
			else:
				num = create_forgot_email(request, myuser = myuser)
				if num == 1:
					return redirect('core:confirm_forgot_email',email = email)
				else:
					messages.error(request, "There was a problem sending your confirmation email.  Please try again.")
					return redirect('core:signup')

		else:
			messages.error(request, "Email does not exist.")
			return redirect('core:forgotPassEmail')

	return render(request,'core/forgotPassEmail.html')

def create_forgot_email(request, myuser):

	sender_email = config('SENDER_EMAIL')
	sender_name = "The Chosen Fantasy Games"
	sender_password = config('SENDER_PASSWORD')
	receiver_email = myuser.username

	smtp_server = config('SMTP_SERVER')
	smtp_port = config('SMTP_PORT')

	current_site = get_current_site(request)

	message = MIMEMultipart()
	message['From'] = f"{sender_name} <{sender_email}>"
	message['To'] = receiver_email
	message['Subject'] = "Change Your Password for The Chosen"
	body = render_to_string('core/email_change.html',{
		'domain' : current_site.domain,
		'uid' : urlsafe_base64_encode(force_bytes(myuser.pk)),
		'token' : generate_token.make_token(myuser),
		})
	message.attach(MIMEText(body, "html"))
	text = message.as_string()
	try:
		server = smtplib.SMTP(smtp_server, smtp_port)
		server.starttls()  # Secure the connection
		server.login(sender_email, sender_password)
		server.sendmail(sender_email, receiver_email, text)
	except Exception as e:
		print(f"Failed to send email: {e}")
		messages.error(request, "There was a problem sending your email.  Please try again.")
		return 2
		#redirect('signup')
	finally:
		server.quit()

	return 1


def confirm_forgot_email(request, email):
	user = User.objects.get(username = email)
	if request.method == "POST":
		create_forgot_email(request, myuser = user)
	return render(request, "core/confirm_forgot_email.html",{"email":email})

def passreset(request, uidb64, token):
	try:
		uid = force_str(urlsafe_base64_decode(uidb64))
		myuser = User.objects.get(pk=uid)
	except (TypeError, ValueError, OverflowError, User.DoesNotExist):
		myuser = None
	if myuser is not None and generate_token.check_token(myuser,token):

		if request.method == "POST":
			pass1 = request.POST.get('password1')
			pass2 = request.POST.get('password2')
			if pass1 == pass2:
				myuser.set_password(pass1)
				myuser.save()
				return redirect('core:signin')
			else:
				messages.error(request,"Passwords do not match.")
				return redirect('core:passreset',uidb64=uidb64,token=token)
	return render(request,'core/passreset.html',{'uidb64':uidb64,'token':token})
