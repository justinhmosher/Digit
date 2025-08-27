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

def debug_session(request):
    return JsonResponse({"keys": list(request.session.keys())}, safe=False)

def homepage(request):
	return render(request,"core/homepage.html")

def _generate_code(n=6):
    return "".join(str(random.randint(0,9)) for _ in range(n))

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


@require_POST
def request_otp(request):
    data = json.loads(request.body.decode() or "{}")
    phone_raw = (data.get("phone") or "").strip()
    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid phone number."}, status=400)
    try:
        resp = send_sms_otp(phone_e164)
        # print("VERIFY RESEND ->", phone_e164, resp.sid, resp.status)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to resend SMS: {e}"}, status=500)
    return JsonResponse({"ok": True, "message": "OTP re-sent", "phone_e164": phone_e164})

@require_POST
def verify_otp(request):
    data = json.loads(request.body.decode() or "{}")
    phone_raw = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()

    if not phone_raw or not code:
        return JsonResponse({"ok": False, "error": "Phone and code are required."}, status=400)

    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid phone."}, status=400)

    # 1) Verify PHONE via Verify
    try:
        status = check_sms_otp(phone_e164, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)

    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # Mark phone as verified and fetch user
    try:
        profile = CustomerProfile.objects.select_related('user').get(phone=phone_e164)
    except CustomerProfile.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Profile not found."}, status=400)

    # optional flags
    if hasattr(profile, "phone_verified"):
        profile.phone_verified = True
        profile.save(update_fields=["phone_verified"])

    # 2) Kick off EMAIL verification
    email = (profile.user.email or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "error": "User has no email on file."}, status=400)

    try:
        send_email_otp(email)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Could not send email code: {e}"}, status=500)

    # Tell FE to switch to email step
    return JsonResponse({
        "ok": True,
        "stage": "email",
        "email": email,
        "message": "Phone verified. We sent a 6-digit code to your email."
    })

@require_POST
def verify_email_otp(request):
    data = json.loads(request.body.decode() or "{}")
    email = (data.get("email") or "").strip().lower()
    code  = (data.get("code") or "").strip()

    if not email or not code:
        return JsonResponse({"ok": False, "error": "Email and code are required."}, status=400)

    # Verify EMAIL via Verify
    try:
        status = check_email_otp(email, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)

    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # Mark flags + activate user
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return JsonResponse({"ok": False, "error": "User not found."}, status=400)

    profile = CustomerProfile.objects.filter(user=user).first()
    if profile and hasattr(profile, "email_verified"):
        profile.email_verified = True
        profile.save(update_fields=["email_verified"])

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])

    return JsonResponse({
        "ok": True,
        "message": "Email verified. Please sign in.",
        "redirect": "/profile"
    })

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

@require_POST
def owner_signup_api(request):
    """
    Body JSON: {first_name, last_name, email, phone, username, password1, password2}
    Do NOT create the user yet. Send phone OTP, stash data in session.
    """
    data = json.loads(request.body.decode() or "{}")
    first_name = (data.get("first_name") or "").strip()
    last_name  = (data.get("last_name") or "").strip()
    email      = (data.get("email") or "").strip().lower()
    phone_raw  = (data.get("phone") or "").strip()
    username   = (data.get("username") or "").strip().lower()
    p1 = data.get("password1") or ""
    p2 = data.get("password2") or ""

    if not (first_name and last_name and email and phone_raw and username and p1 and p2):
        return JsonResponse({"ok": False, "error": "All fields are required."}, status=400)
    if p1 != p2:
        return JsonResponse({"ok": False, "error": "Passwords didn't match."}, status=400)
    if User.objects.filter(username=username).exists():
        return JsonResponse({"ok": False, "error": "Username already taken."}, status=400)
    if User.objects.filter(email=email).exists():
        return JsonResponse({"ok": False, "error": "Email already registered."}, status=400)

    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    # Send phone OTP
    try:
        send_sms_otp(phone_e164)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Failed to send SMS: {e}"}, status=500)

    # Stash into session until OTPs complete
    request.session["owner_signup"] = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone_e164,
        "username": username,
        "password1": p1,
    }
    request.session.modified = True

    return JsonResponse({"ok": True, "stage": "phone", "phone_e164": phone_e164})

@require_POST
def owner_verify_phone_api(request):
    """
    Body: {code}
    On success, send email OTP and return stage=email.
    """
    payload = json.loads(request.body.decode() or "{}")
    code = (payload.get("code") or "").strip()
    ss = request.session.get("owner_signup")
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Start again."}, status=400)
    phone_e164 = ss["phone"]

    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    try:
        status = check_sms_otp(phone_e164, code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    # Send email OTP now
    try:
        send_email_otp(ss["email"])
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Could not send email code: {e}"}, status=500)

    # Flag phone verified in session
    ss["phone_verified"] = True
    request.session["owner_signup"] = ss
    request.session.modified = True

    return JsonResponse({"ok": True, "stage": "email", "email": ss["email"]})

@require_POST
def owner_verify_email_api(request):
    """
    Body: {code}
    On success, redirect to /owner/restaurant (HTML form). Still no User created yet.
    """
    payload = json.loads(request.body.decode() or "{}")
    code = (payload.get("code") or "").strip()
    ss = request.session.get("owner_signup")
    if not ss:
        return JsonResponse({"ok": False, "error": "Session expired. Start again."}, status=400)

    if not code:
        return JsonResponse({"ok": False, "error": "Enter the 6-digit code."}, status=400)

    try:
        status = check_email_otp(ss["email"], code)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)
    if status != "approved":
        return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)

    ss["email_verified"] = True
    request.session["owner_signup"] = ss
    request.session.modified = True

    return JsonResponse({"ok": True, "redirect": "/owner/restaurant"})

def owner_restaurant_page(request):
    # must have passed both OTPs
    ss = request.session.get("owner_signup")
    if not (ss and ss.get("phone_verified") and ss.get("email_verified")):
        return HttpResponseBadRequest("Complete verification first.")
    return render(request, "core/owner_restaurant.html")

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
    role = request.session.pop("auth_role", None)
    if role == "owner":
        rp = getattr(request.user, "restaurant_profile", None)
        if not rp:
            return redirect("core:restaurant_onboard")
        return redirect("core:owner_dashboard")
    return redirect("core:profile")

# core/views.py (add near your other imports)
from datetime import timedelta
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.core.mail import send_mail
from django.conf import settings

from .models import ManagerInvite  # assumes you already have this model

@login_required
@require_POST
def owner_invite_manager(request):
    """
    Owner sends an invite to a manager's email.
    Accepts JSON or form-POST.

    POST fields:
      - email (required)
      - expires_minutes (optional, default 120)
    """
    # Owner must have a RestaurantProfile
    rp = getattr(request.user, "restaurant_profile", None)
    if not rp:
        return JsonResponse(
            {"ok": False, "error": "Create your restaurant profile first."},
            status=400,
        )

    # Read payload from JSON or form
    email = ""
    expires_minutes = 120
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
        email = (payload.get("email") or "").strip().lower()
        if payload.get("expires_minutes") is not None:
            expires_minutes = int(payload["expires_minutes"])
    except Exception:
        pass

    if not email:
        email = (request.POST.get("email") or "").strip().lower()
    if request.POST.get("expires_minutes"):
        try:
            expires_minutes = int(request.POST.get("expires_minutes"))
        except ValueError:
            pass

    if not email:
        return JsonResponse({"ok": False, "error": "Please provide an email."}, status=400)

    # Create invite
    invite = ManagerInvite.objects.create(
        restaurant=rp,
        email=email,
        expires_at=timezone.now() + timedelta(minutes=expires_minutes),
    )

    # Build link
    invite_link = f"{request.scheme}://{request.get_host()}/manager/accept?token={invite.token}"

    # Send a simple email (okay for MVP)
    subject = "You’re invited as a manager"
    rest_name = rp.dba_name or rp.legal_name or "your restaurant"
    body = (
        f"You’ve been invited to manage {rest_name} on Dine N Dash.\n\n"
        f"Click to accept your invite and set your password:\n{invite_link}\n\n"
        f"This link expires at {invite.expires_at:%Y-%m-%d %H:%M}."
    )
    try:
        send_manager_invite_email(
            to_email=email,
            invite_link=invite_link,
            restaurant_name=rest_name,
            expires_at=invite.expires_at,
        )
        email_ok = True
    except Exception as e:
        # Log e in real life; don’t fail the API in MVP unless you want to
        return JsonResponse({"ok": False, "error": f"Email send failed: {e}"}, status=500)

    return JsonResponse({
        "ok": True,
        "message": f"Invite {'sent' if email_ok else 'created (email failed)'} to {email}.",
        "invite": {
            "email": email,
            "token": invite.token,
            "expires_at": invite.expires_at.isoformat(),
            "link": invite_link,
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


@require_http_methods(["GET", "POST"])
def manager_accept_invite(request):
    """
    GET  /manager/accept?token=... -> render accept form
    POST /manager/accept          -> JSON result
    """
    # ---------- GET: render form ----------
    if request.method == "GET":
        token = request.GET.get("token")
        if not token:
            return render(request, "core/manager_accept_invalid.html", status=400)
        try:
            invite = ManagerInvite.objects.select_related("restaurant").get(token=token)
        except ManagerInvite.DoesNotExist:
            return render(request, "core/manager_accept_invalid.html", status=404)
        if not invite.is_valid:
            return render(request, "core/manager_accept_invalid.html", status=400)
        return render(request, "core/manager_accept.html", {"invite": invite})

    # ---------- POST: JSON or form ----------
    # Parse JSON first so we can read 'token' from the body.
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
    except Exception:
        payload = {}

    token = (
        payload.get("token")
        or request.POST.get("token")
        or request.GET.get("token")
    )
    if not token:
        return JsonResponse({"ok": False, "error": "Missing invite token."}, status=400)

    try:
        invite = ManagerInvite.objects.select_related("restaurant").get(token=token)
    except ManagerInvite.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Invite not found."}, status=400)
    if not invite.is_valid:
        return JsonResponse({"ok": False, "error": "Invite is expired or already used."}, status=400)

    # Pull fields from JSON (fallback to form)
    email     = (payload.get("email") or request.POST.get("email") or "").strip().lower()
    phone     = (payload.get("phone") or request.POST.get("phone") or "").strip()
    password1 = (payload.get("password1") or request.POST.get("password1") or "").strip()
    password2 = (payload.get("password2") or request.POST.get("password2") or "").strip()

    if email != invite.email.lower():
        return JsonResponse({"ok": False, "error": "Email must match the invited address."}, status=400)
    if not phone:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)
    if not password1 or password1 != password2:
        return JsonResponse({"ok": False, "error": "Passwords must match."}, status=400)

    # Create/activate the user
    user = User.objects.filter(username=email).first()
    if user:
        user.email = email
        user.set_password(password1)
        user.is_active = True
        user.save()
    else:
        user = User.objects.create_user(username=email, email=email, password=password1)
        user.is_active = True
        user.save()

    # Create/update ManagerProfile and attach restaurant
    try:
        mp, _ = ManagerProfile.objects.get_or_create(user=user)
        mp.phone = phone
        mp.restaurant = invite.restaurant
        mp.save()
    except IntegrityError:
        # e.g., unique phone collision
        return JsonResponse({"ok": False, "error": "Phone number already in use."}, status=400)

    # Mark invite accepted
    invite.accepted_at = timezone.now()
    invite.save(update_fields=["accepted_at"])

    # Log in and send destination
    login(request, user)
    return JsonResponse({"ok": True, "redirect": "/manager/dashboard"})

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

def restaurant_signin(request):
    return render(request, "core/restaurant_signin.html", {"active_tab": active_tab})


def restaurant_signin(request):
    """GET: render the page. POST: JSON sign-in for owner/manager."""
    if request.method == "GET":
        # define it unconditionally to avoid NameError (UI also reads ?tab=manager)
        active_tab = request.GET.get("tab", "owner")
        return render(request, "core/restaurant_signin.html", {"active_tab": active_tab})

    # --- POST -> JSON ---
    portal = (request.POST.get("portal") or "").strip() or "owner"   # "owner" | "manager"
    email  = (request.POST.get("email") or "").strip().lower()
    pwd    = request.POST.get("password") or ""

    if not email or not pwd:
        return JsonResponse({"ok": False, "error": "Email and password are required."}, status=400)

    user = authenticate(request, username=email, password=pwd)
    if not user:
        return JsonResponse({"ok": False, "error": "Invalid email or password."}, status=400)

    login(request, user)
    # Decide destination
    if portal == "manager":
        dest = reverse("core:manager_dashboard")
    else:
        dest = reverse("core:owner_dashboard") if hasattr(user, "restaurant_profile") else reverse("core:restaurant_onboard")

    return JsonResponse({"ok": True, "redirect": dest})

@login_required
def owner_dashboard(request):
    rp = getattr(request.user, "restaurant_profile", None)
    return render(request, "core/owner_dashboard.html", {"profile": rp})

# core/views.py
from urllib.parse import quote
from django.shortcuts import redirect

def owner_google_start(request):
    """
    Mark this flow as 'owner' before handing off to Google OAuth.
    """
    request.session["auth_role"] = "owner"
    request.session.modified = True

    next_url = request.GET.get("next", "/post-login-owner/")  # wherever you want to land after OAuth
    # Hand off directly to Allauth’s Google login endpoint
    return redirect(f"/accounts/google/login/?process=login&next={quote(next_url)}")


@login_required
def manager_dashboard(request):
    mp = getattr(request.user, "manager_profile", None)
    return render(request, "core/manager_dashboard.html", {"profile": mp})

@require_http_methods(["GET","POST"])
@login_required
def restaurant_onboard(request):
    rp = getattr(request.user, "restaurant_profile", None)
    if rp:
        return redirect("core:owner_dashboard")
    if request.method == "GET":
        rp = getattr(request.user, "restaurant_profile", None)
        return render(request, "core/restaurant_onboard.html", {"profile": rp})

    # POST JSON
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

    rp, _ = RestaurantProfile.objects.get_or_create(user=request.user)
    rp.legal_name = legal_name
    rp.email      = email
    rp.dba_name   = dba_name
    rp.phone      = phone
    rp.address    = address
    rp.is_active  = True  # mark as configured (tune to your rules)
    rp.save()

    return JsonResponse({"ok": True, "redirect": "/owner/dashboard/"})



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
