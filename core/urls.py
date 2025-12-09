from django.contrib import admin
from django.urls import path,include
from django.views.generic import RedirectView
from . import views, views_staff, views_home, veiws_verify, views_payments, views_add_staff, views_manager, views_owner,views_resetpin, views_restaurants, views_auth_reset
from django.conf import settings
from django.conf.urls.static import static

app_name = 'core'

urlpatterns = [
	path('',views_home.customer_home,name="homepage"),
    path("home",views.discovery,name="home"),
    path("terms", views.terms,name="terms"),
    path("about", views.about_us,name="about_us"),
    path("privacy", views.privacy,name="privacy"),
    path("customer/signup", views.signup, name="signup"),
	path("api/customer/begin",views.customer_begin_api,name="customer_begin_api"),
    path('customer/precheck/',views.customer_precheck_api, name ="customer_precheck_api"),
	path('signin',views.signin,name="signin"),
	path('signout',views_home.signout,name="signout"),
    path('customer/google/start',views.customer_google_start, name = "customer_google_start"),
    path('post/login/customer',views.post_login_customer,name = "post_login_customer"),
	path('profile',views_home.customer_home,name='profile'),
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    path("auth/verify-email",views.verify_email_otp, name = "verify_email_otp"),
    path('restaurant/signin',views.restaurant_signin, name = 'restaurant_signin'),
    path("restaurants/connect/start",  views_restaurants.connect_onboard_start,  name="connect_onboard_start"),
    path("restaurants/connect/return", views_restaurants.connect_onboard_return, name="connect_onboard_return"),
    path("restaurants/connect/dashboard", views_restaurants.connect_dashboard_login, name="connect_dashboard_login"),
    path("owner/signup/existing", views.owner_begin_existing_api, name = "owner_begin_existing_api"),
    path("owner/verify/existing", views.owner_existing_verify_phone_api, name = "owner_existing_verify_phone_api"),
    path("api/reviews", views_home.api_submit_review, name="api_submit_review"),
    # dashboards
    # OWNER (standard, JSON-only)
    path("owner/signup", views.owner_signup, name="owner_signup"),        # renders HTML shell
    path("owner/signup/api", views.owner_signup_api, name="owner_signup_api"),      # POST JSON, sends phone OTP
    path("owner/otp/verify", views.owner_verify_phone_api, name="owner_verify_phone_api"),
    path("owner/email/verify", views.owner_verify_email_api, name="owner_verify_email_api"),
    path("owner/contact/", views.owner_contact, name="owner_contact"),
    #path("owner/restaurant", views.owner_restaurant_page, name="owner_restaurant_page"),  # renders form
    path("owner/restaurant/save", views.owner_restaurant_save_api, name="owner_restaurant_save_api"),
    path("owner/", views_owner.owner_dashboard, name="owner_dashboard"),
    path("auth/reset/start",    views_auth_reset.reset_start,    name="auth_reset_start"),
    path("auth/reset/verify",   views_auth_reset.reset_verify,   name="auth_reset_verify"),
    path("auth/reset/pin",      views_auth_reset.reset_pin,      name="auth_reset_pin"),
    path("auth/reset/finalize", views_auth_reset.reset_finalize, name="auth_reset_finalize"),

    path("owner/api/state", views_owner.owner_api_state, name="owner_api_state"),
    path("owner/api/set-restaurant", views_owner.owner_api_set_restaurant, name="owner_api_set_restaurant"),
    path("owner/api/add-restaurant", views_owner.owner_api_add_restaurant, name="owner_api_add_restaurant"),
    path("owner/api/remove-restaurant", views_owner.owner_api_remove_restaurant, name="owner_api_remove_restaurant"),

    path("owner/api/remove-manager", views_owner.owner_api_remove_manager, name="owner_api_remove_manager"),
    path("owner/api/add-owner", views_owner.owner_api_add_owner, name="owner_api_add_owner"),
    path("owner/api/remove-owner", views_owner.owner_api_remove_owner, name="owner_api_remove_owner"),

    path("owner/api/ticket/<str:ticket_id>", views_owner.owner_api_ticket_detail, name="owner_api_ticket_detail"),
    path("owner/invite-manager", views_owner.owner_invite_manager, name="owner_invite_manager"),
    path("owner/export", views_owner.owner_export, name="owner_export"),
    path("owner/api/remove-staff", views_owner.owner_api_remove_staff, name="owner_api_remove_staff"),
    path("owner/invite-staff",     views_owner.owner_invite_staff,     name="owner_invite_staff"),

    # Google path A — phone page & JSON endpoints
    path("oauth/phone", views.oauth_phone_page, name="oauth_phone_page"),
    path("oauth/phone/init", views.oauth_phone_init, name="oauth_phone_init"),        # POST phone → send OTP
    path("oauth/phone/verify", views.oauth_phone_verify, name="oauth_phone_verify"),
    path("oauth/verify/existing",views.oauth_verify_existing, name = "oauth_verify_existing"),
    path("post-login-owner/", views.post_login_owner, name="post_login_owner"),
    path('owner/precheck/', views.owner_precheck_api, name = "owner_precheck_api"),
    path("owner/google-start/", views.owner_google_start, name="owner_google_start"),
    path("manager/google-start", views.manager_google_start, name = "manager_google_start"),
    path("owner/oauth/phone", views.oauth_owner_phone_page, name="oauth_owner_phone_page"),
    path("owner/oauth/phone/init", views.oauth_owner_phone_init, name="oauth_owner_phone_init"),
    path("owner/oauth/phone/verify", views.oauth_owner_phone_verify, name="oauth_owner_phone_verify"),
    path("manager/OTP/verify",views.manager_accept_verify, name = "manager_accept_verify"),
    path("manager/accept", views.manager_accept, name="manager_accept"),
    path("manager/dashboard/", views_manager.manager_dashboard, name="manager_dashboard"),
    path("manager/api/state", views_manager.manager_api_state, name="manager_api_state"),
    path("manager/api/staff/remove", views_manager.manager_api_remove_staff, name="manager_api_remove_staff"),
    path("manager/api/ticket/<str:ticket_id>", views_manager.manager_api_ticket_detail, name="manager_api_ticket_detail"),
    path("manager/export", views_manager.manager_export, name="manager_export"),
    # existing classic path B endpoints you already have
    path("auth/verify-otp", views.verify_otp, name="verify_otp"),
    # core/views.py
    path("debug/session", views.debug_session),
    path("api/precheck-user", views.precheck_user_api, name="precheck_user_api"),
    path("api/link-member", views_staff.api_link_member_to_ticket, name="link_member"),
    path("api/member/<str:member_number>/receipt", views_home.api_ticket_receipt,name="customer_ticket_receipt"),
    path("verify/<member>/", veiws_verify.verify_member, name="verify_member"),
    path("staff/state", views_staff.api_staff_board_state, name="staff_board_state"),
    path("add-card/", views_payments.add_card, name="add_card"),
    path("set-pin/", views_payments.set_pin, name="set_pin"),
    path("save-pin/", views_payments.save_pin_finalize, name="save_pin_finalize"),
    path("staff/", views_staff.staff_console, name="staff_console"),
    path("staff/api/board", views_staff.api_staff_board_state, name="staff_board_state"),
    path("staff/api/link-member", views_staff.api_link_member_to_ticket, name="link_member"),
    #path("staff/api/close-ticket", views_staff.api_staff_close_ticket, name="staff_close_ticket"),
    path("owner/ticket-review/<int:ticket_link_id>/",views_owner.owner_ticket_review_json,name="owner_ticket_review_json",),
    path("verify/<str:member>", veiws_verify.verify_member, name="verify_member"),
    path("api/receipt/<str:member_number>", views_staff.api_ticket_receipt, name="ticket_receipt"),
    path("manager/invite-staff", views_add_staff.manager_invite_staff, name="manager_invite_staff"),
    path("staff/accept", views_add_staff.staff_accept, name="staff_accept"),
    path("staff/accept/verify", views_add_staff.staff_accept_verify, name="staff_accept_verify"),
    path("staff/google-start",views_add_staff.staff_google_start, name="staff_google_start"),
    path("staff/api/close", views_staff.api_staff_close_ticket, name="staff_close_ticket"),
    path("api/member/<str:member>/close", views_home.api_close_tab, name="member_close_tab"),
    path("staff/api/resend", views_staff.api_staff_resend_link, name="staff_resend_link"),
    path("staff/api/cancel", views_staff.api_staff_cancel_link, name="staff_cancel_link"),
    path("owner/OTP/verify",views_owner.owner_accept_verify, name = "owner_accept_verify"),
    path("owner/accept", views_owner.owner_accept, name="owner_accept"),
    path("stripe/webhook/owner/", views_restaurants.stripe_owner_webhook, name="stripe_owner_webhook"),
    path("owner/api/menu-item-ratings/", views_owner.owner_api_menu_item_ratings, name="owner_api_menu_item_ratings"),
    path("owner/api/staff-ratings/", views_owner.owner_api_staff_ratings, name="owner_api_staff_ratings"),
    path("owner_api_staff_ratings_debug", views_owner.owner_api_staff_ratings_debug, name="owner_api_staff_ratings_debug"),
    path("api/me/transactions", views_home.api_me_transactions, name="api_me_transactions"),
    path("api/tickets/<int:tl_id>", views_home.api_ticket_link_receipt, name="api_ticket_link_receipt"),
    path("api/reviews", views_home.api_review_submit, name="api_review_submit"),
    path("api/review/submit", views_home.api_review_submit, name="api_review_submit_legacy"),  # supports both URLs used in JS
    path("api/reviews/for-ticket/<int:ticket_link_id>", views_home.api_review_for_ticket, name="api_review_for_ticket"),
    path("api/reviews", views_home.api_review_save, name="api_review_save"),  # POST create/update
    path("payments/update", views_payments.update_card, name="update_card"),
    path("payments/update/confirm-pin", views_payments.update_card_confirm_pin, name="update_card_confirm_pin"),
    path("payments/update/finalize", views_payments.finalize_card_update, name="finalize_card_update"),
    # Manager analytics
    path("manager/api/menu-item-ratings", views_manager.manager_api_menu_item_ratings, name="manager_api_menu_item_ratings"),
    path("manager/api/staff-ratings", views_manager.manager_api_staff_ratings, name="manager_api_staff_ratings"),
    path("manager/ticket/<int:ticket_link_id>/review.json",views_manager.manager_ticket_review_json,name="manager_ticket_review_json"),
    path("reset-pin/<str:token>/", views_resetpin.reset_pin_confirm, name="reset_pin_confirm"),
]
