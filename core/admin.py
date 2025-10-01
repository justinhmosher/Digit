from django.contrib import admin
from .models import RestaurantProfile,ManagerProfile,ManagerInvite, CustomerProfile, OwnerProfile, Ownership, Member, TicketLink, StaffInvite, StaffProfile, Review

admin.site.register(ManagerInvite)
admin.site.register(StaffInvite)
admin.site.register(CustomerProfile)
admin.site.register(Ownership)
# core/admin.py
from django.contrib import admin
from .models import RestaurantProfile, OwnerProfile, Ownership, ManagerProfile

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
