# ---------- imports ----------
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods, require_POST
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.utils import timezone
from django.urls import reverse
from django.contrib.auth import login
from django.contrib.auth.models import User
from datetime import timedelta
import json

# app-level imports (adjust paths to match your project)
from .models import StaffInvite, StaffProfile, OwnerProfile, CustomerProfile, ManagerProfile
from .utils import send_staff_invite_email, send_sms_otp, check_sms_otp, to_e164_us
from urllib.parse import urlencode, quote


def _current_restaurant(request):
    """
    Resolve the current restaurant strictly via the signed-in ManagerProfile.
    Returns a RestaurantProfile instance or None.

    Also caches the id in session for convenience.
    """
    # Prefer the related object already on request.user
    mp = getattr(request.user, "manager_profile", None)
    if not mp or not getattr(mp, "restaurant_id", None):
        mp = ManagerProfile.objects.select_related("restaurant").filter(user=request.user).first()

    if mp and mp.restaurant_id:
        rp = mp.restaurant
        # cache for later convenience
        if request.session.get("current_restaurant_id") != rp.id:
            request.session["current_restaurant_id"] = rp.id
            request.session.modified = True
        return rp

    return None


@login_required
@require_http_methods(["GET", "POST"])
def manager_invite_staff(request):
    """Owner sends an invite for the *current* restaurant."""
    rp = _current_restaurant(request)
    if request.method == "GET":
        if not rp:
            return JsonResponse({"ok": False, "error": "Create your restaurant profile first."}, status=400)
        return render(request, "core/owner_invite_staff.html", {"restaurant": rp})

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

    invite = StaffInvite.objects.create(
        restaurant=rp,
        email=email,
        expires_at=timezone.now() + timedelta(minutes=expires_minutes),
    )

    # Build accept link with next=/staff/
    accept_url = request.build_absolute_uri(reverse("core:staff_accept"))
    next_url   = reverse("core:staff_console")  # -> /staff/
    link = f"{accept_url}?{urlencode({'token': str(invite.token), 'next': next_url})}"

    rest_name = rp.dba_name or rp.legal_name or "your restaurant"

    try:
        send_staff_invite_email(
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


def find_verified_phone(user):
    """Return a phone already verified for this user (any profile)."""
    if not user:
        return None
    for M in (StaffProfile, OwnerProfile, CustomerProfile, ManagerProfile):
        prof = M.objects.filter(user=user).first()
        if prof and getattr(prof, "phone", None):
            if getattr(prof, "phone_verified", True):  # default to True if field missing
                return prof.phone
    return None


def mask(phone):
    return f"•••{phone[-4:]}" if phone and len(phone) >= 4 else ""


@require_http_methods(["GET", "POST"])
def staff_accept(request):
    """
    Staff invite flow:
    - GET: if verified phone on file → send OTP + show code page; else show phone form.
    - POST (phone form): normalize phone, (create password if needed), send OTP, show code page.
    """
    token = request.GET.get("token") or request.POST.get("token") or ""
    invite = StaffInvite.objects.filter(token=token).first()
    if not _invite_is_valid(invite):
        return render(request, "core/staff_accept_invalid.html")

    # figure out where to go after success (default to /staff/)
    default_next = reverse("core:staff_console")
    next_url = request.GET.get("next") or request.POST.get("next") or default_next

    restaurant_name = invite.restaurant.dba_name or invite.restaurant.legal_name
    user = User.objects.filter(email__iexact=invite.email).first()
    existing_user = bool(user)
    onfile_phone = find_verified_phone(user)

    if request.method == "GET":
        if onfile_phone:
            # auto-send OTP and stash context
            send_sms_otp(onfile_phone)
            request.session["staff_accept"] = {
                "token": token,
                "email": invite.email.lower(),
                "phone": onfile_phone,
                "existing": existing_user,
                "next": next_url,
            }
            return render(
                request,
                "core/staff_accept_code.html",
                {
                    "token": token,
                    "email": invite.email,
                    "phone_mask": mask(onfile_phone),
                    "restaurant_name": restaurant_name,
                    "next": next_url,
                },
            )
        else:
            # remember next so POST can retrieve it even if the form doesn’t carry it
            request.session["staff_accept_next"] = next_url
            return render(
                request,
                "core/staff_accept_phone.html",
                {
                    "token": token,
                    "email": invite.email,
                    "need_password": not existing_user,
                    "restaurant_name": restaurant_name,
                    "next": next_url,
                },
            )

    # -------- POST (phone form) --------
    phone_raw = (request.POST.get("phone") or "").strip()
    if not phone_raw:
        return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)
    try:
        phone_e164 = to_e164_us(phone_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Enter a valid US phone number."}, status=400)

    password = None
    if not existing_user:
        p1 = request.POST.get("password1") or ""
        p2 = request.POST.get("password2") or ""
        if not p1 or p1 != p2:
            return JsonResponse({"ok": False, "error": "Passwords didn't match."}, status=400)
        password = p1

    # recover next if not posted
    next_url = request.session.pop("staff_accept_next", None) or next_url

    send_sms_otp(phone_e164)
    request.session["staff_accept"] = {
        "token": token,
        "email": invite.email.lower(),
        "phone": phone_e164,
        "existing": existing_user,
        "password": password,
        "next": next_url,
    }
    return render(
        request,
        "core/staff_accept_code.html",
        {
            "token": token,
            "email": invite.email,
            "phone_mask": mask(phone_e164),
            "restaurant_name": restaurant_name,
            "next": next_url,
        },
    )



@require_POST
def staff_accept_verify(request):
    """Verify OTP, create user/profile if needed, accept invite, log in."""
    try:
        data = json.loads(request.body.decode() or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "Bad JSON."}, status=400)

    stash = request.session.get("staff_accept") or {}
    if not stash or stash.get("token") != data.get("token"):
        return JsonResponse({"ok": False, "error": "Session expired. Restart from invite link."}, status=400)

    code = (data.get("code") or "").strip()
    if not code:
        return JsonResponse({"ok": False, "error": "Missing code."}, status=400)

    try:
        if check_sms_otp(stash["phone"], code) != "approved":
            return JsonResponse({"ok": False, "error": "Invalid or expired code."}, status=400)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Verification error: {e}"}, status=500)

    email = stash["email"]
    user = User.objects.filter(email__iexact=email).first()
    if not user:
        user = User.objects.create_user(username=email, email=email, password=stash.get("password"))
        user.is_active = True
        user.save()

    # create/attach staff profile
    sp, _ = StaffProfile.objects.get_or_create(user=user)
    if not getattr(sp, "phone", None):
        sp.phone = stash["phone"]
    if hasattr(sp, "phone_verified"):
        sp.phone_verified = True
    if hasattr(sp, "email_verified"):
        sp.email_verified = True
    if not getattr(sp, "restaurant_id", None):
        invite = StaffInvite.objects.filter(token=stash["token"]).first()
        if invite:
            sp.restaurant = invite.restaurant
            invite.accepted_at = timezone.now()
            invite.save(update_fields=["accepted_at"])
    sp.save()

    target = stash.get("next") or reverse("core:staff_console")
    del request.session["staff_accept"]
    login(request, user)
    return JsonResponse({"ok": True, "redirect": target})



def staff_google_start(request):
    request.session["auth_role"] = "staff"
    request.session.modified = True
    next_url = reverse("core:staff_console") + "?role=staff"
    return redirect(f"/accounts/google/login/?process=login&next={quote(next_url)}")