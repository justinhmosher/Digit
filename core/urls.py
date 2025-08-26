from django.contrib import admin
from django.urls import path,include
from django.views.generic import RedirectView
from . import views
from django.conf import settings
from django.conf.urls.static import static

app_name = 'core'

urlpatterns = [
	path('',views.homepage,name="homepage"),
	path('signup',views.signup,name="signup"),
	path('signin',views.signin,name="signin"),
	path('signout',views.signout,name="signout"),
	path('profile',views.profile,name='profile'),
	path("auth/request-otp", views.request_otp, name="request_otp"),
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    path("auth/verify-email",views.verify_email_otp, name = "verify_email_otp"),
    path('restaurant/signin',views.restaurant_signin, name = 'restaurant_signin'),
    # dashboards
    # OWNER (standard, JSON-only)
    path("owner/signup", views.owner_signup, name="owner_signup"),        # renders HTML shell
    path("owner/signup/api", views.owner_signup_api, name="owner_signup_api"),      # POST JSON, sends phone OTP
    path("owner/otp/verify", views.owner_verify_phone_api, name="owner_verify_phone_api"),
    path("owner/email/verify", views.owner_verify_email_api, name="owner_verify_email_api"),
    path("owner/restaurant", views.owner_restaurant_page, name="owner_restaurant_page"),  # renders form
    path("owner/restaurant/save", views.owner_restaurant_save_api, name="owner_restaurant_save_api"),
    path("owner/dashboard/", views.owner_dashboard, name="owner_dashboard"),
    # Google path A — phone page & JSON endpoints
    path("oauth/phone", views.oauth_phone_page, name="oauth_phone_page"),
    path("oauth/phone/init", views.oauth_phone_init, name="oauth_phone_init"),        # POST phone → send OTP
    path("oauth/phone/verify", views.oauth_phone_verify, name="oauth_phone_verify"), 
    path("post-login-owner/", views.post_login_owner, name="post_login_owner"),
    path("owner/invite-manager/", views.owner_invite_manager, name="owner_invite_manager"),
    path("owner/google-start/", views.owner_google_start, name="owner_google_start"),
    
    # existing classic path B endpoints you already have
    path("auth/request-otp", views.request_otp, name="request_otp"),
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    # core/views.py
    path("restaurant/onboard",views.restaurant_onboard,name = "restaurant_onboard"),
    path("debug/session", views.debug_session)
]