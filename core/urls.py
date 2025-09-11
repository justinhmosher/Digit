from django.contrib import admin
from django.urls import path,include
from django.views.generic import RedirectView
from . import views, views_staff, views_home, veiws_verify, views_payments
from django.conf import settings
from django.conf.urls.static import static

app_name = 'core'

urlpatterns = [
	path('',views_home.customer_home,name="homepage"),
    path("customer/signup", views.signup, name="signup"),
	path('customer/signup/api/',views.customer_begin_api,name="customer_begin_api"),
    path('customer/precheck/',views.customer_precheck_api, name ="customer_precheck_api"),
	path('signin',views.signin,name="signin"),
	path('signout',views.signout,name="signout"),
    path('customer/google/start',views.customer_google_start, name = "customer_google_start"),
    path('post/login/customer',views.post_login_customer,name = "post_login_customer"),
	path('profile',views.profile,name='profile'),
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    path("auth/verify-email",views.verify_email_otp, name = "verify_email_otp"),
    path('restaurant/signin',views.restaurant_signin, name = 'restaurant_signin'),
    # dashboards
    # OWNER (standard, JSON-only)
    path("owner/signup", views.owner_signup, name="owner_signup"),        # renders HTML shell
    path("owner/signup/api", views.owner_signup_api, name="owner_signup_api"),      # POST JSON, sends phone OTP
    path("owner/otp/verify", views.owner_verify_phone_api, name="owner_verify_phone_api"),
    path("owner/email/verify", views.owner_verify_email_api, name="owner_verify_email_api"),
    #path("owner/restaurant", views.owner_restaurant_page, name="owner_restaurant_page"),  # renders form
    path("owner/restaurant/save", views.owner_restaurant_save_api, name="owner_restaurant_save_api"),
    path("owner/dashboard/", views.owner_dashboard, name="owner_dashboard"),
    path("owner/signup/existing", views.owner_begin_existing_api, name = "owner_begin_existing_api"),
    path("owner/existing/phone/verify", views.owner_existing_verify_phone_api, name = "owner_existing_verify_phone_api"),
    # Google path A — phone page & JSON endpoints
    path("oauth/phone", views.oauth_phone_page, name="oauth_phone_page"),
    path("oauth/phone/init", views.oauth_phone_init, name="oauth_phone_init"),        # POST phone → send OTP
    path("oauth/phone/verify", views.oauth_phone_verify, name="oauth_phone_verify"),
    path("oauth/verify/existing",views.oauth_verify_existing, name = "oauth_verify_existing"),
    path("post-login-owner/", views.post_login_owner, name="post_login_owner"),
    path('owner/precheck/', views.owner_precheck_api, name = "owner_precheck_api"),
    path("owner/invite-manager/", views.owner_invite_manager, name="owner_invite_manager"),
    path("owner/google-start/", views.owner_google_start, name="owner_google_start"),
    path("manager/google-start", views.manager_google_start, name = "manager_google_start"),
    path("owner/oauth/phone", views.oauth_owner_phone_page, name="oauth_owner_phone_page"),
    path("owner/oauth/phone/init", views.oauth_owner_phone_init, name="oauth_owner_phone_init"),
    path("owner/oauth/phone/verify", views.oauth_owner_phone_verify, name="oauth_owner_phone_verify"),
    path("manager/OTP/verify",views.manager_accept_verify, name = "manager_accept_verify"),
    path("manager/accept", views.manager_accept, name="manager_accept"),
    path("manager/dashboard/", views.manager_dashboard, name="manager_dashboard"),
    # existing classic path B endpoints you already have
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    # core/views.py
    path("restaurant/onboard",views.restaurant_onboard,name = "restaurant_onboard"),
    path("debug/session", views.debug_session),
    path("api/precheck-user", views.precheck_user_api, name="precheck_user_api"),
    path("staff", views_staff.staff_console, name="staff_console"),
    path("api/link-member", views_staff.api_link_member_to_ticket, name="link_member"),
    path("api/ticket/<member>/receipt", views_staff.api_ticket_receipt, name="ticket_receipt"),
    path("api/ticket/<member>/close", views_staff.api_close_tab, name="close_tab"),
    path("verify/<member>/", veiws_verify.verify_member, name="verify_member"),
    path("staff/state", views_staff.api_staff_board_state, name="staff_board_state"),
    path("set-pin/", views_payments.set_pin, name="set_pin"),
    path("save-pin/", views_payments.save_pin_finalize, name="save_pin_finalize"),

]
