from django.contrib import admin
from .models import RestaurantProfile,ManagerProfile,ManagerInvite

admin.site.register(RestaurantProfile)
admin.site.register(ManagerProfile)
admin.site.register(ManagerInvite)
