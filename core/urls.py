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
	path('activate/<uidb64>/<token>',views.activate,name='activate'),
	path('forgotPassEmail',views.forgotPassEmail,name='forgotPassEmail'),
	path('passreset/<uidb64>/<token>',views.passreset,name='passreset'),
	path('confirm-email/<str:email>/',views.confirm_email,name='confirm_email'),
	path('confirm-forgot-email/<str:email>/',views.confirm_forgot_email,name='confirm_forgot_email'),
	path('profile',views.profile,name='profile'),
	path("auth/request-otp", views.request_otp, name="request_otp"),
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),

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
]