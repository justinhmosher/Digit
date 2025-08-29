from django.contrib import admin
from .models import RestaurantProfile,ManagerProfile,ManagerInvite, CustomerProfile, OwnerProfile, Ownership

admin.site.register(OwnerProfile)
admin.site.register(RestaurantProfile)
admin.site.register(ManagerProfile)
admin.site.register(ManagerInvite)
admin.site.register(CustomerProfile)
admin.site.register(Ownership)