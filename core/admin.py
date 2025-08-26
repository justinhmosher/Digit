from django.contrib import admin
from .models import RestaurantProfile,ManagerProfile,ManagerInvite, CustomerProfile, OwnerProfile

admin.site.register(OwnerProfile)
admin.site.register(RestaurantProfile)
admin.site.register(ManagerProfile)
admin.site.register(ManagerInvite)
admin.site.register(CustomerProfile)
