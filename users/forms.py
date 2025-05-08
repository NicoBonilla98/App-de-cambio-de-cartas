from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import CustomUser, Card

class UserRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = CustomUser
        fields = ['username', 'email', 'password1', 'password2']

class CardForm(forms.ModelForm):
    class Meta:
        model = Card
        fields = ['name', 'description']

class UploadFileForm(forms.Form):
    file = forms.FileField(label='Selecciona un archivo TXT')