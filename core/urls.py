from django.contrib import admin
from django.urls import path,include
from django.views.generic import RedirectView
from . import views
from django.conf import settings
from django.conf.urls.static import static

app_name = 'core'

urlpatterns = [
	path('',views.homepage,name="homepage"),
	path('signup',views.signup,name="signup"),
	path('signin',views.signin,name="signin"),
	path('signout',views.signout,name="signout"),
	path('activate/<uidb64>/<token>',views.activate,name='activate'),
	path('forgotPassEmail',views.forgotPassEmail,name='forgotPassEmail'),
	path('passreset/<uidb64>/<token>',views.passreset,name='passreset'),
	path('confirm-email/<str:email>/',views.confirm_email,name='confirm_email'),
	path('confirm-forgot-email/<str:email>/',views.confirm_forgot_email,name='confirm_forgot_email'),
	path('questions',views.questions,name='questions')
]