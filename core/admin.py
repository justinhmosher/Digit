from django.contrib import admin, messages
from .models import RestaurantProfile,ManagerProfile,ManagerInvite, PinResetToken, CustomerProfile, OwnerProfile, Ownership, Member, TicketLink, StaffInvite, StaffProfile, Review

admin.site.register(ManagerInvite)
admin.site.register(StaffInvite)
admin.site.register(Ownership)
# core/admin.py
from django.contrib import admin
from .models import RestaurantProfile, OwnerProfile, Ownership, ManagerProfile

from .views_resetpin import create_customer_pin_reset  # uses the helper you wrote
from .utils import send_customer_pin_reset_email       # your SendGrid sender

class OwnershipInline(admin.TabularInline):
    model = Ownership
    extra = 0
    autocomplete_fields = ["owner"]

@admin.register(RestaurantProfile)
class RestaurantProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "dba_name", "legal_name", "email", "is_active", "created_at")
    search_fields = ("dba_name", "legal_name", "email")
    inlines = [OwnershipInline]

@admin.register(OwnerProfile)
class OwnerProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "phone", "phone_verified", "email_verified")
    search_fields = ['user__email','user__username']
    autocomplete_fields = ['user']

@admin.register(ManagerProfile)
class ManagerProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "restaurant", "phone", "phone_verified")

admin.site.register(Member)
admin.site.register(TicketLink)

@admin.register(StaffProfile)
class ManagerProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "restaurant", "phone", "phone_verified")

@admin.action(description="Send PIN reset link to selected customers")
def send_customer_pin_reset(modeladmin, request, queryset):
    sent, skipped = 0, 0
    for customer in queryset.select_related("user"):
        user = getattr(customer, "user", None)
        email = getattr(user, "email", None)
        if not user or not email:
            skipped += 1
            continue

        reset_url, expires_at, _ = create_customer_pin_reset(customer, request)
        send_customer_pin_reset_email(
            to_email=email,
            reset_link=reset_url,
            customer_name=(user.get_full_name() or user.username or "there"),
            expires_at=expires_at,
        )
        sent += 1

    if sent:
        messages.success(request, f"Sent {sent} PIN reset link(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} customer(s) without an email.")

# Attach action + keep your list columns sane
@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "phone", "phone_verified", "email_verified")
    search_fields = ("user__username", "user__email", "phone")
    actions = [send_customer_pin_reset]


# Optional: expose tokens in admin (read-only)
@admin.register(PinResetToken)
class PinResetTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "used", "expires_at", "created_at")
    list_filter = ("used",)
    search_fields = ("customer__user__username", "customer__user__email")
    readonly_fields = ("customer", "token_hash", "created_at", "expires_at", "used")


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "restaurant", "member_display", "stars", "short_comment", "created_at")
    list_select_related = ("restaurant", "member")
    list_filter = ("stars", "restaurant")
    search_fields = ("member__number", "restaurant__dba_name", "restaurant__legal_name", "comment")

    def member_display(self, obj):
        return getattr(obj.member, "number", "—")
    member_display.short_description = "Member"

    def short_comment(self, obj):
        text = obj.comment or ""
        return (text[:60] + "…") if len(text) > 60 else text
    short_comment.short_description = "Comment"
