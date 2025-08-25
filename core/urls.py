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

    # restaurant auth
    path("restaurant/signin", views.restaurant_signin, name="restaurant_signin"),
    path("owner/signup/", views.owner_signup, name="owner_signup"),
    # dashboards
    path("owner/dashboard/", views.owner_dashboard, name="owner_dashboard"),
    path("manager/dashboard/", views.manager_dashboard, name="manager_dashboard"),
    path("restaurant/onboard/", views.restaurant_onboard, name="restaurant_onboard"),
    # manager invites
    path("owner/managers/invite", views.owner_invite_manager, name="owner_invite_manager"),
    path("manager/accept", views.manager_accept_invite, name="manager_accept_invite"),
    # Google path A — phone page & JSON endpoints
    path("oauth/phone", views.oauth_phone_page, name="oauth_phone_page"),
    path("oauth/phone/init", views.oauth_phone_init, name="oauth_phone_init"),        # POST phone → send OTP
    path("oauth/phone/verify", views.oauth_phone_verify, name="oauth_phone_verify"),  # POST code → finish signup

    # existing classic path B endpoints you already have
    path("auth/request-otp", views.request_otp, name="request_otp"),
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    path("debug/session", views.debug_session)
]