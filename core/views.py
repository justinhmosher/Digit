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
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.conf import settings
from .utils import send_sms_otp, to_e164_us, check_sms_otp, send_email_otp, check_email_otp
from allauth.socialaccount.models import SocialLogin
from allauth.socialaccount.helpers import complete_social_login
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.account.utils import perform_login

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


def owner_signup(request):
    """
    Similar to your customer signup, but we also capture basic restaurant info
    and create a RestaurantProfile immediately (status is pending until Stripe).
    """
    if request.method == "POST":
        email = (request.POST.get('email') or "").strip().lower()
        password1 = request.POST.get('password1')
        password2 = request.POST.get('password2')

        # extra owner questions
        legal_name = (request.POST.get("legal_name") or "").strip()
        dba_name = (request.POST.get("dba_name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        address = (request.POST.get("address") or "").strip()

        if password1 != password2:
            messages.error(request, "Passwords didn't match!")
            return redirect('core:owner_signup')

        if not legal_name:
            messages.error(request, "Please enter your restaurant's legal name.")
            return redirect('core:owner_signup')

        myuser = User.objects.filter(email=email).first()
        if myuser:
        	if myuser.is_active:
        		pass
        	else:
        		myuser.username = username
        		myuser.email = email
        		myuser.set_password(password1)
        		myuser.is_active = False
        		myuser.save()
        else:
        	myuser = User.objects.create_user(username, email, password1)
        	myuser.is_active = False
        	myuser.save()

        owner = RestaurantProfile.objects.filter(email=email).first()
        if owner:
        	if owner.is_active:
        		messages.error(request, "Email already registered with an active account! Please try another.")
        		return redirect('core:owner_signup')
        	else:
        		rp, created = RestaurantProfile.objects.get_or_create(
            		user = myuser,
            		defaults={
            			"email":email,
            			"legal_name": legal_name,
            			"dba_name":dba_name,
            			"phone": phone,
            			"address": address,
            			"is_active": False,
            		})
        		if not created:
        			rp.email = email
        			rp.legal_name = legal_name
        			rp.dba_name = dba_name
        			rp.phone = phone
        			rp.address = address
        			rp.is_active = False
        else:
        	rp, created = RestaurantProfile.objects.get_or_create(
            	user = myuser,
            	defaults={
            		"email":email,
            		"legal_name": legal_name,
            		"dba_name":dba_name,
            		"phone": phone,
            		"address": address,
            		"is_active": False,
            		},
            	)
        	if not created:
        		rp.email = email
        		rp.legal_name = legal_name
        		rp.dba_name = dba_name
        		rp.phone = phone
        		rp.address = address
        		rp.is_active = False

        num = create_email(request, myuser, "owner")
        if num == 1:
            return redirect('core:confirm_email', email=email)
        else:
            messages.error(request, "There was a problem sending your confirmation email. Please try again.")
            return redirect('core:owner_signup')

    # GET
    return render(request, "core/owner_signup.html")



# ---------- Restaurant Sign In (Owner | Manager tabs) ----------
def restaurant_signin(request):
    if request.method == "POST":
        portal = (request.POST.get("portal") or "owner").strip()
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""

        user = authenticate(request, username=email, password=password)
        if not user:
            messages.error(request, "Invalid email or password.")
            return redirect("core:restaurant_signin")

        login(request, user)

        if portal == "manager":
            return redirect("core:manager_dashboard")
        else:
            if hasattr(user, "restaurant_profile"):
                return redirect("core:owner_dashboard")
            return redirect("core:restaurant_onboard")

    active_tab = request.GET.get("tab", "owner")
    return render(request, "core/restaurant_signin.html", {"active_tab": active_tab})

# ---------- Owner Sign Up (same as before; omitted here for brevity) ----------
# def owner_signup(request): ... (use the version from previous message)

# ---------- Dashboards (same placeholders as before) ----------
@login_required
def owner_dashboard(request):
    rp = getattr(request.user, "restaurant_profile", None)
    return render(request, "core/owner_dashboard.html", {"profile": rp})

@login_required
def restaurant_onboard(request):
    rp = getattr(request.user, "restaurant_profile", None)
    if not rp:
        return redirect("core:owner_signup")
    return render(request, "core/restaurant_onboard.html", {"profile": rp})

@login_required
def manager_dashboard(request):
    mp = getattr(request.user, "manager_profile", None)
    return render(request, "core/manager_dashboard.html", {"profile": mp})

# ---------- Owner → Invite Manager ----------
@login_required
def owner_invite_manager(request):
    """
    POST only. Owner sends an invite to a manager's email.
    Body (form): email, full_name (optional), expires_minutes (optional)
    """
    if request.method != "POST":
        return redirect("core:owner_dashboard")

    if not hasattr(request.user, "restaurant_profile"):
        messages.error(request, "Create your restaurant profile first.")
        return redirect("core:owner_signup")

    email = (request.POST.get("email") or "").strip().lower()
    expires_minutes = int(request.POST.get("expires_minutes") or 120)

    if not email:
        messages.error(request, "Please provide an email to invite.")
        return redirect("core:owner_dashboard")

    rp = request.user.restaurant_profile
    invite = ManagerInvite.objects.create(
        restaurant=rp,
        email=email,
        expires_at=timezone.now() + timedelta(minutes=expires_minutes)
    )

    invite_link = f"{request.scheme}://{request.get_host()}/manager/accept?token={invite.token}"
    subject = "You’re invited as a manager"
    body = (
        f"You’ve been invited to manage {rp.dba_name or rp.legal_name} on Dine N Dash.\n\n"
        f"Click to accept and set your password:\n{invite_link}\n\n"
        f"This link expires at {invite.expires_at}."
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=True)
    except Exception:
        pass  # fine for MVP

    messages.success(request, f"Invite sent to {email}.")
    return redirect("core:owner_dashboard")

# ---------- Manager → Accept Invite ----------
def manager_accept_invite(request):
    """
    GET: Show a small form to set name/phone/password using the token.
    POST: Create/activate the manager user + ManagerProfile, mark invite accepted, log in.
    """
    token = request.GET.get("token") or request.POST.get("token")
    if not token:
        return render(request, "core/manager_accept_invalid.html")

    try:
        invite = ManagerInvite.objects.get(token=token)
    except ManagerInvite.DoesNotExist:
        return render(request, "core/manager_accept_invalid.html")

    if not invite.is_valid:
        return render(request, "core/manager_accept_invalid.html")

    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        password1 = request.POST.get("password1") or ""
        password2 = request.POST.get("password2") or ""
        full_name = (request.POST.get("full_name") or "").strip()
        phone     = (request.POST.get("phone") or "").strip()

        if email != invite.email.lower():
            messages.error(request, "Email must match the invited address.")
            return redirect(f"/manager/accept?token={invite.token}")

        if password1 != password2:
            messages.error(request, "Passwords didn't match.")
            return redirect(f"/manager/accept?token={invite.token}")

        user = User.objects.filter(username=email).first()
        if user:
            # If a dormant user exists, update password & activate
            user.email = email
            user.set_password(password1)
            user.is_active = True
            user.save()
        else:
            user = User.objects.create_user(email, email, password1)
            user.is_active = True
            user.save()

        # ensure ManagerProfile
        if not hasattr(user, "manager_profile"):
            ManagerProfile.objects.create(user=user, full_name=full_name or email, phone=phone)

        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])

        login(request, user)
        return redirect("core:manager_dashboard")

    # GET
    return render(request, "core/manager_accept.html", {"invite": invite})



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

def profile(request):
	return render(request,'core/profile.html')