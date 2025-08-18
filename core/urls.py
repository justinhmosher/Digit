from django.contrib import admin
from django.urls import path,include
from django.views.generic import RedirectView
from . import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
	path('',views.homepage,name="homepage"),
	path('signup',views.signup,name="signup"),
	path('signin',views.signin,name="signin"),
	path('signout',views.signout,name="signout"),
	path('activate/<uidb64>/<token>',views.activate,name='activate'),
	path('forgotPassEmail',views.forgotPassEmail,name='forgotPassEmail'),
	path('passreset/<uidb64>/<token>',views.passreset,name='passreset'),
	path('questions',views.questions,name='questions')
]