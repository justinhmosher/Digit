# core/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.encoding import force_str
import uuid, hashlib

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

    def __str__(self):
        return force_str(getattr(self, "label", None) or f"CustomerProfile {self.pk}")


# ---------- Restaurants & Ownership ----------
class RestaurantProfile(models.Model):
    legal_name = models.CharField(max_length=255)
    dba_name   = models.CharField(max_length=255, blank=True)
    email      = models.EmailField()
    phone      = models.CharField(max_length=30, blank=True)
    address    = models.TextField(blank=True)
    is_active  = models.BooleanField(default=False)

    # timestamps (handy for ordering)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The **only** link to owners: a M2M through Ownership
    owners = models.ManyToManyField(
        'OwnerProfile',
        through='Ownership',
        related_name='restaurants'          # lets you do: owner.restaurants.all()
    )

    def __str__(self):
        return self.dba_name or self.legal_name


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


class PhoneOTP(models.Model):
    PURPOSES  = (("login","login"), ("signup","signup"))
    phone     = models.CharField(max_length=20, db_index=True)
    purpose   = models.CharField(max_length=10, choices=PURPOSES, default="signup")
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts   = models.PositiveIntegerField(default=0)
    is_used    = models.BooleanField(default=False)

    def is_expired(self):
        return timezone.now() >= self.expires_at

    @staticmethod
    def hash_code(code: str) -> str:
        salt = os.environ.get("OTP_SALT", "change_me_salt")
        return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()

class Member(models.Model):
    # your global member
    number = models.CharField(max_length=20, unique=True, db_index=True)
    last_name = models.CharField(max_length=64)
    customer = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE)
    # optional: link to a CustomerProfile/User, phone, etc.

class TicketLink(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    location_id = models.CharField(max_length=64)      # Omnivore location
    ticket_id   = models.CharField(max_length=64, db_index=True)
    server_name = models.CharField(max_length=64, blank=True)
    status      = models.CharField(max_length=16, default="open")  # open|closing|closed
    opened_at   = models.DateTimeField(auto_now_add=True)
    closed_at   = models.DateTimeField(null=True, blank=True)
    last_total  = models.IntegerField(default=0)  # cents
    external_txn_id = models.CharField(max_length=64, blank=True)  # if you charge via Stripe, etc.


