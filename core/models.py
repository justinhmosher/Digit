# core/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.encoding import force_str
import uuid, hashlib
from django.db.models import Q

# ---------- Profiles ----------
class OwnerProfile(models.Model):
    user           = models.OneToOneField(User, on_delete=models.CASCADE, related_name="owner_profile")
    phone          = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)

    def __str__(self):
        return force_str(getattr(self, "label", None) or f"OwnerProfile {self.pk}")


class CustomerProfile(models.Model):
    user           = models.OneToOneField(User, on_delete=models.CASCADE, related_name="customer_profile")
    phone          = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    stripe_customer_id     = models.CharField(max_length=64, blank=True)
    default_payment_method = models.CharField(max_length=64, blank=True)
    pin_hash = models.CharField(max_length=128, blank=True, null=True) 

    def __str__(self):
        return force_str(getattr(self, "label", None) or f"CustomerProfile {self.pk}")


class RestaurantProfile(models.Model):
    # Keep your existing fields if you want (they’ll become “non-authoritative”)
    legal_name = models.CharField(max_length=255, blank=True)
    dba_name   = models.CharField(max_length=255, blank=True)
    email      = models.EmailField(blank=True)
    phone      = models.CharField(max_length=30, blank=True)

    # (Old address fields can remain; we won’t rely on them)
    addr_line1 = models.CharField(max_length=160, blank=True)
    addr_line2 = models.CharField(max_length=160, blank=True)
    city       = models.CharField(max_length=80,  blank=True)
    state      = models.CharField(max_length=32,  blank=True)
    postal     = models.CharField(max_length=32,  blank=True)

    omnivore_location_id = models.CharField(max_length=64, blank=True)

    # ✅ New: the only Stripe id you truly need for restaurants
    stripe_account_id = models.CharField(max_length=64, blank=True)   # acct_*

    # Optional cache of Stripe account details (so UI can render fast)
    stripe_cached = models.JSONField(default=dict, blank=True)

    is_active  = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    owners = models.ManyToManyField('OwnerProfile', through='Ownership', related_name='restaurants')
    menu_cache           = models.JSONField(default=list, blank=True)   # [{id, name, price_cents, category, in_stock}]
    menu_cache_synced_at = models.DateTimeField(null=True, blank=True)

    staff_cache           = models.JSONField(default=list, blank=True)  # [{id, name, check_name, role, is_active}]
    staff_cache_synced_at = models.DateTimeField(null=True, blank=True)

    def display_name(self):
        dba = (self.stripe_cached or {}).get("business_profile", {}).get("name") or ""
        return dba or self.dba_name or self.legal_name or f"Restaurant {self.pk}"
    def __str__(self): return self.display_name()


class Ownership(models.Model):
    """Through table for RestaurantProfile <-> OwnerProfile"""
    owner      = models.ForeignKey(OwnerProfile, on_delete=models.CASCADE, related_name="ownerships")
    restaurant = models.ForeignKey(RestaurantProfile, on_delete=models.CASCADE, related_name="ownerships")
    role       = models.CharField(max_length=30, blank=True)  # optional (e.g., 'primary')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("owner", "restaurant")

    def __str__(self):
        return f"{self.owner_id} -> {self.restaurant_id}"


# ---------- Managers ----------
class ManagerProfile(models.Model):
    user           = models.OneToOneField(User, on_delete=models.CASCADE, related_name="manager_profile")
    phone          = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    restaurant     = models.ForeignKey(
        RestaurantProfile, on_delete=models.CASCADE,
        related_name="managers", null=True, blank=True
    )

    def __str__(self):
        return force_str(getattr(self, "label", None) or f"ManagerProfile {self.pk}")


# ---------- Invites / OTP (unchanged) ----------
class ManagerInvite(models.Model):
    restaurant  = models.ForeignKey(RestaurantProfile, on_delete=models.CASCADE, related_name="manager_invites")
    email       = models.EmailField()
    token       = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at  = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        return self.accepted_at is None and self.expires_at > timezone.now()

class OwnerInvite(models.Model):
    restaurant  = models.ForeignKey(RestaurantProfile, on_delete=models.CASCADE, related_name="owner_invites")
    email       = models.EmailField()
    token       = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at  = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        return self.accepted_at is None and self.expires_at > timezone.now()

# ---------- Staff ----------
class StaffProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="staff_profile"          # was "manager_profile" -> clashes
    )
    phone          = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    restaurant     = models.ForeignKey(
        RestaurantProfile,
        on_delete=models.CASCADE,
        related_name="staff_members",         # was "managers" -> clashes
        null=True, blank=True
    )

    def __str__(self):
        return force_str(getattr(self, "label", None) or f"StaffProfile {self.pk}")


class StaffInvite(models.Model):
    restaurant  = models.ForeignKey(
        RestaurantProfile,
        on_delete=models.CASCADE,
        related_name="staff_invites"          # was "manager_invites" -> clashes
    )
    email       = models.EmailField()
    token       = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at  = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    @property
    def is_valid(self):
        return self.accepted_at is None and self.expires_at > timezone.now()

class Member(models.Model):
    # your global member
    number = models.CharField(max_length=20, unique=True, db_index=True)
    last_name = models.CharField(max_length=64)
    customer = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE)
    # optional: link to a CustomerProfile/User, phone, etc.

class TicketLink(models.Model):
    STATUS = (("pending","Pending"),("open","Open"),("closed","Closed"))

    member        = models.ForeignKey("Member", on_delete=models.CASCADE, related_name="ticket_links")
    restaurant    = models.ForeignKey("RestaurantProfile", on_delete=models.PROTECT, related_name="ticket_links")
    ticket_id     = models.CharField(max_length=64, db_index=True)
    ticket_number = models.CharField(max_length=32, blank=True)
    table         = models.CharField(max_length=64, blank=True)
    server_name   = models.CharField(max_length=120, blank=True)

    status            = models.CharField(max_length=12, choices=STATUS, default="pending", db_index=True)
    last_total_cents  = models.IntegerField(default=0)
    currency          = models.CharField(max_length=8, default="USD")
    opened_at         = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at         = models.DateTimeField(null=True, blank=True, db_index=True)

    # snapshot (from RestaurantProfile at close; POS fallback if empty)
    merchant_name  = models.CharField(max_length=160, blank=True)
    merchant_addr1 = models.CharField(max_length=160, blank=True)
    merchant_addr2 = models.CharField(max_length=160, blank=True)
    merchant_city  = models.CharField(max_length=80,  blank=True)
    merchant_state = models.CharField(max_length=32,  blank=True)
    merchant_zip   = models.CharField(max_length=32,  blank=True)
    merchant_phone = models.CharField(max_length=32,  blank=True)

    pos_created_at  = models.DateTimeField(null=True, blank=True)
    pos_settled_at  = models.DateTimeField(null=True, blank=True)

    items_json      = models.JSONField(default=list, blank=True)
    discounts_cents = models.IntegerField(default=0)
    subtotal_cents  = models.IntegerField(default=0)
    tax_cents       = models.IntegerField(default=0)
    tip_cents       = models.IntegerField(default=0)
    total_cents     = models.IntegerField(default=0)
    paid_cents      = models.IntegerField(default=0)
    change_cents    = models.IntegerField(default=0)

    tender_brand   = models.CharField(max_length=32,  blank=True)
    tender_last4   = models.CharField(max_length=8,   blank=True)
    tender_type    = models.CharField(max_length=64,  blank=True)
    auth_code      = models.CharField(max_length=64,  blank=True)
    pos_payment_id = models.CharField(max_length=64,  blank=True)
    pos_ref        = models.CharField(max_length=128, blank=True)

    raw_ticket_json   = models.JSONField(default=dict, blank=True)
    raw_payments_json = models.JSONField(default=list,  blank=True)

    emailed_to   = models.EmailField(blank=True)
    emailed_at   = models.DateTimeField(null=True, blank=True)
    email_status = models.CharField(max_length=40, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status","opened_at"]),
            models.Index(fields=["status","closed_at"]),
            models.Index(fields=["ticket_id","status"]),
            models.Index(fields=["member","status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["member","restaurant","ticket_id","status"],
                name="uniq_member_restaurant_ticket_by_status"
            )
        ]

    def __str__(self):
        return f"{self.member_id} · {self.restaurant.display_name()} · {self.ticket_number or self.ticket_id} · {self.status}"

class Review(models.Model):
    restaurant   = models.ForeignKey(RestaurantProfile, on_delete=models.CASCADE, related_name="reviews")
    ticket_link  = models.ForeignKey("TicketLink", null=True, blank=True, on_delete=models.SET_NULL, related_name="reviews")
    member       = models.ForeignKey("Member", null=True, blank=True, on_delete=models.SET_NULL, related_name="reviews")

    stars        = models.PositiveSmallIntegerField()  # 1..5
    comment      = models.TextField(blank=True)

    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Prevent duplicate reviews per ticket (when we have a ticket)
            models.UniqueConstraint(
                fields=["ticket_link"],
                condition=Q(ticket_link__isnull=False),
                name="uniq_review_per_ticketlink",
            ),
        ]

    def clean(self):
        if not (1 <= int(self.stars) <= 5):
            raise ValueError("Stars must be between 1 and 5")

    def __str__(self):
        who = self.member_id or "anon"
        return f"Review({self.stars}★) for {self.restaurant_id} by {who}"




