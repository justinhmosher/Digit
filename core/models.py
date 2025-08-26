from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import uuid
import hashlib, os
from django.utils.encoding import force_str


class CustomerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    def __str__(self):
        return force_str(getattr(self, "label", None) or f"CustomerProfile {self.pk}")

class ManagerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    def __str__(self):
        return force_str(getattr(self, "label", None) or f"ManagerProfile {self.pk}")

class OwnerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=32, unique=True)
    phone_verified = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    def __str__(self):
        return force_str(getattr(self, "label", None) or f"OwnerProfile {self.pk}")

class RestaurantProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="restaurant_profile")
    legal_name = models.CharField(max_length=255)
    email = models.CharField(max_length=255, default = "email")
    dba_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    processor = models.CharField(max_length=20, default="stripe")
    processor_account_id = models.CharField(max_length=64, blank=True, null=True, unique=True)
    processor_verification = models.CharField(max_length=32, default="pending")
    payout_status = models.CharField(max_length=32, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default = False)

    def __str__(self):
        return self.dba_name or self.legal_name

class ManagerInvite(models.Model):
    """Owner invites a manager by email. One-time link with expiry."""
    restaurant = models.ForeignKey(RestaurantProfile, on_delete=models.CASCADE, related_name="manager_invites")
    email      = models.EmailField()
    token      = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_valid(self):
        return self.accepted_at is None and self.expires_at > timezone.now()

class PhoneOTP(models.Model):
    PURPOSES = (("login","login"), ("signup","signup"))
    phone = models.CharField(max_length=20, db_index=True)
    purpose = models.CharField(max_length=10, choices=PURPOSES, default="signup")
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    is_used = models.BooleanField(default=False)

    def is_expired(self):
        return timezone.now() >= self.expires_at

    @staticmethod
    def hash_code(code:str) -> str:
        salt = os.environ.get("OTP_SALT", "change_me_salt")
        return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


