# filepath: /Users/usuario/Documents/Proyectos python/my_django_project/users/models.py
from django.db import models
from django.contrib.auth.models import User

class Card(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name

class UserCard(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_cards")
    card = models.ForeignKey(Card, on_delete=models.CASCADE)
    is_owned = models.BooleanField(default=False)  # True = en posesión, False = deseada

    def __str__(self):
        return f"{self.user.username} - {self.card.name} ({'Poseída' if self.is_owned else 'Deseada'})"

# filepath: /Users/usuario/Documents/Proyectos python/my_django_project/users/views.py
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import Card, UserCard

@login_required
def add_card(request, card_id, is_owned):
    card = get_object_or_404(Card, id=card_id)
    UserCard.objects.create(user=request.user, card=card, is_owned=bool(is_owned))
    return redirect('card_list')