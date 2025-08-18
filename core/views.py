from Clover import settings
from django.shortcuts import redirect, render, get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.models import User
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.mail import send_mail
import win32com.client as win32
import pythoncom
from . tokens import generate_token
from django.contrib.sites.shortcuts import get_current_site
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.template.loader import render_to_string
import requests
from decouple import config
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Count,F,ExpressionWrapper,fields
from datetime import datetime
from itertools import chain
from collections import defaultdict
from datetime import datetime
from django.utils import timezone
import json

def homepage(request):
	return render(request,"digit/homepage.html")

def signup(request):
	
	if request.method == "POST":

		email = request.POST.get('email')
		password1 = request.POST.get('password1')
		password2 = request.POST.get('password2')

		if User.objects.filter(email = email):
			messages.error(request, "Email already registered!  Please try another.")
			return redirect('signup')


		if password1 != password2:
			messages.error(request,"Passwors didn't match!")
			return redirect('signup')

		username = email
		myuser = User.objects.create_user(username,email, password1)
		myuser.is_active = False

		myuser.save()


		messages.success(request, "Your Account has been successfully created!  We have sent you a confirmation email, please confirm your email in order to activate your account.")

		#Welcome Email

		olApp = win32.Dispatch('Outlook.Application',pythoncom.CoInitialize())
		olNS = olApp.GetNameSpace('MAPI')

		mail_item = olApp.createItem(0)

		mail_item.Subject = "Welcome to Clover"
		mail_item.BodyFormat = 1

		mail_item.Body = "Hello player !! \n" + "\nWelcome to Clover!! \n Thank you for joining. \n We sent you a confirmation email, please confirm your email address in order to activate your account! \n\n Thank You."
		mail_item.Sender = "commissioner@bigballzdfsl.com"
		mail_item.To = myuser.email

		mail_item.Display()
		mail_item.Save()
		mail_item.Send()

		#Email Address Confirmation Email

		mail_item1 = olApp.createItem(0)

		current_site = get_current_site(request)
		mail_item1.Subject = "Confirm your email for Clover"
		mail_item1.BodyFormat = 1
		mail_item1.Body = render_to_string('digit/email_confirmation.html',{
			'domain' : current_site.domain,
			'uid' : urlsafe_base64_encode(force_bytes(myuser.pk)),
			'token' : generate_token.make_token(myuser),
			})
		mail_item1.Sender = "commissioner@bigballzdfsl.com"
		mail_item1.To = myuser.email
		mail_item1.Save()
		mail_item1.Send()

		return redirect('signin')

	return render(request,"digit/signup.html")

def activate(request, uidb64, token):
	try:
		uid = force_str(urlsafe_base64_decode(uidb64))
		myuser = User.objects.get(pk=uid)
	except (TypeError, ValueError, OverflowError, User.DoesNotExist):
		myuser = None

	if myuser is not None and generate_token.check_token(myuser,token):
		myuser.is_active = True
		myuser.save()
		login(request, myuser)
		return redirect('signin')
	else:
		return render(request, 'digit/activation_failed.html')	

def signin(request):

	if request.method == 'POST':
		username = request.POST.get('email')
		password1 = request.POST.get('password1')

		user = authenticate(username = username, password = password1)

		if user is not None:
			login(request, user)
			return render(request,'digit/main.html')

		else:
			messages.error(request, "Bad Credentials!")
			return redirect('signin')	

	return render(request, "digit/signin.html")

def signout(request):
	logout(request)
	messages.success(request, "Logged Out Successfully")
	return redirect('homepage')

def forgotPassEmail(request):
	if request.method == "POST":
		username = request.POST.get('email')

		if User.objects.filter(username=username).exists():

			myuser = User.objects.get(username = username)

			olApp = win32.Dispatch('Outlook.Application',pythoncom.CoInitialize())
			olNS = olApp.GetNameSpace('MAPI')

			mail_item1 = olApp.createItem(0)

			current_site = get_current_site(request)

			mail_item1.Subject = "Confirm your email for Clover"
			mail_item1.BodyFormat = 1
			mail_item1.Body = render_to_string('digit/email_change.html',{
				'domain' : current_site.domain,
				'uid' : urlsafe_base64_encode(force_bytes(myuser.pk)),
				'token' : generate_token.make_token(myuser),
				})

			mail_item1.Sender = "commissioner@bigballzdfsl.com"
			mail_item1.To = email

			mail_item1.Display()
			mail_item1.Save()
			mail_item1.Send()

			messages.success(request,"We sent password change instructions over email")
			return redirect('forgotPassEmail')
		else:
			messages.error(request,"Please provide a valid email")

	return render(request,'digit/forgotPassEmail.html')

def passreset(request, uidb64, token):
	try:
		uid = force_str(urlsafe_base64_decode(uidb64))
		myuser = User.objects.get(pk=uid)
	except (TypeError, ValueError, OverflowError, User.DoesNotExist):
		myuser = None
	if myuser is not None and generate_token.check_token(myuser,token):

		if request.method == "POST":
			pass1 = request.POST.get('password1')
			pass2 = request.POST.get('password2')
			if pass1 == pass2:
				myuser.set_password(pass1)
				myuser.save()

				return redirect('signin')
			else:
				messages.error("Passwors do not match")
				return redirect('passreset',uidb64=uidb64,token=token)
	return render(request,'digit/passreset.html',{'uidb64':uidb64,'token':token})

def questions(request):
	return render(request,'digit/questions.html')