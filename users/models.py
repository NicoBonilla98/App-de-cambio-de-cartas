from django.db import models
from django.contrib.auth.models import AbstractUser

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
        null=True
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
        null=True
    )


class Card(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)  # New field for price

    def __str__(self):
        return self.name

class UserCard(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="user_cards")
    card = models.ForeignKey(Card, on_delete=models.CASCADE)
    is_owned = models.BooleanField(default=False)  # True = en posesión, False = deseada
    quantity_owned = models.PositiveIntegerField(default=0)
    quantity_required = models.PositiveIntegerField(default=0)

    def total_price(self):
        return self.card.price * self.quantity_owned if self.is_owned else 0

    def __str__(self):
        return f"{self.user.username} - {self.card.name} ({'Poseída' if self.is_owned else 'Deseada'})"


class Exchange(models.Model):
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_exchanges')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_exchanges')
    sender_cards = models.TextField()  # Lista de cartas enviadas por el remitente
    receiver_cards = models.TextField()  # Lista de cartas enviadas por el receptor
    date = models.DateTimeField(auto_now_add=True)
    STATUS_CHOICES = [
        ('pending', 'Pendiente'),
        ('accepted', 'Aceptado'),
        ('rejected', 'Rechazado'),
    ]
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='pending'
    )
    EXCHANGE_TYPE_CHOICES = [
        ('sale', 'Venta'),
        ('trade', 'Cambio'),
    ]
    exchange_type = models.CharField(
        max_length=10,
        choices=EXCHANGE_TYPE_CHOICES,
        default='trade'
    )

    def __str__(self):
        return f"Intercambio entre {self.sender.username} y {self.receiver.username} el {self.date}"
    
class Notification(models.Model):
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="sent_notifications")
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="received_notifications")
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    TYPE_CHOICES = [
        ('info', 'Informativo'),
        ('action', 'Acción'),
        ('error', 'Error')
    ]
    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='info')
    transaction = models.ForeignKey(Exchange, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')

    def __str__(self):
        return f"Notificación de {self.sender.username} para {self.receiver.username}: {self.message}"
