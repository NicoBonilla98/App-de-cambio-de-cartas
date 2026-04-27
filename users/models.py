from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    CITY_CHOICES = [
        ('Quito', 'Quito'),
        ('Machala', 'Machala'),
        ('Guayaquil', 'Guayaquil'),
        ('Cuenca', 'Cuenca'),
        ('Ambato', 'Ambato'),
    ]
    city = models.CharField(max_length=50, choices=CITY_CHOICES, default='Quito')
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    PREFERRED_STORE_CHOICES = [
        ('monkey_planet', 'Monkey Planet'),
        ('baul_del_enano', 'Baul del Enano'),
        ('dragonscave', 'Dragonscave'),
        ('camelot', 'Camelot'),
        ('tiempo_de_juegos', 'Tiempo de Juegos'),
    ]
    preferred_store = models.CharField(
        max_length=20,
        choices=PREFERRED_STORE_CHOICES,
        blank=True,
        null=True,
    )
    TRANSACTION_PREFERENCE_CHOICES = [
        ('sell_only', 'Solo Venta'),
        ('trade_only', 'Solo Cambio'),
        ('trade_and_sell', 'Cambio y Venta'),
        ('display_only', 'Solo Display'),
    ]
    transaction_preference = models.CharField(
        max_length=20,
        choices=TRANSACTION_PREFERENCE_CHOICES,
        default='trade_and_sell',
        blank=True,
        null=True,
    )


class Card(models.Model):
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    scryfall_id = models.CharField(max_length=64, unique=True, blank=True, null=True)
    set_name = models.CharField(max_length=120, blank=True, null=True)
    set_code = models.CharField(max_length=16, blank=True, null=True)
    collector_number = models.CharField(max_length=32, blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    rarity = models.CharField(max_length=32, blank=True, null=True)
    usd_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    usd_foil_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    eur_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)

    def __str__(self):
        if self.set_name:
            return f"{self.name} ({self.set_name})"
        return self.name


class UserCard(models.Model):
    LISTING_INTENT_CHOICES = [
        ('sell', 'Venta'),
        ('trade', 'Cambio'),
        ('sell_trade', 'Venta/Cambio'),
    ]
    CONDITION_CHOICES = [
        ('near_mint', 'Near Mint'),
        ('lightly_played', 'Lightly Played'),
        ('moderately_played', 'Moderately Played'),
        ('heavily_played', 'Heavily Played'),
        ('damaged', 'Damaged'),
    ]

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='user_cards')
    card = models.ForeignKey(Card, on_delete=models.CASCADE)
    is_owned = models.BooleanField(default=False)
    quantity_owned = models.PositiveIntegerField(default=0)
    quantity_required = models.PositiveIntegerField(default=0)
    listing_intent = models.CharField(max_length=16, choices=LISTING_INTENT_CHOICES, default='trade')
    condition = models.CharField(max_length=24, choices=CONDITION_CHOICES, default='near_mint')
    asking_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)

    def total_price(self):
        return self.card.price * self.quantity_owned if self.is_owned else 0

    def __str__(self):
        status = 'Poseida' if self.is_owned else 'Deseada'
        return f"{self.user.username} - {self.card.name} ({status})"


class Notification(models.Model):
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_notifications')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_notifications')
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    TYPE_CHOICES = [
        ('info', 'Informativo'),
        ('action', 'Accion'),
        ('error', 'Error'),
    ]
    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='info')

    def __str__(self):
        return f"Notificacion de {self.sender.username} para {self.receiver.username}: {self.message}"


class Exchange(models.Model):
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_exchanges')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_exchanges')
    sender_cards = models.TextField()
    receiver_cards = models.TextField()
    date = models.DateTimeField(auto_now_add=True)
    STATUS_CHOICES = [
        ('pending', 'Pendiente'),
        ('accepted', 'Aceptado'),
        ('rejected', 'Rechazado'),
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    EXCHANGE_TYPE_CHOICES = [
        ('sale', 'Venta'),
        ('trade', 'Cambio'),
    ]
    exchange_type = models.CharField(max_length=10, choices=EXCHANGE_TYPE_CHOICES, default='trade')

    def __str__(self):
        return f"Intercambio entre {self.sender.username} y {self.receiver.username} el {self.date}"
