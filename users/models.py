from django.contrib.auth.models import AbstractUser
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class City(models.Model):
    name = models.CharField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Cities'

    def __str__(self):
        return self.name


class Store(models.Model):
    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name='stores')
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    address = models.CharField(max_length=180, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['city__name', 'name']
        unique_together = [('city', 'name')]

    def __str__(self):
        return f"{self.name} ({self.city.name})"


class CustomUser(AbstractUser):
    city = models.CharField(max_length=80, default='Quito')
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    preferred_store = models.CharField(max_length=100, blank=True, null=True)
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

    def get_preferred_store_display(self):
        if not self.preferred_store:
            return ''
        store = Store.objects.filter(slug=self.preferred_store).select_related('city').first()
        if store:
            return store.name
        legacy_labels = {
            'monkey_planet': 'Monkey Planet',
            'baul_del_enano': 'Baul del Enano',
            'dragonscave': 'Dragonscave',
            'camelot': 'Camelot',
            'tiempo_de_juegos': 'Tiempo de Juegos',
        }
        return legacy_labels.get(self.preferred_store, self.preferred_store)


class Card(models.Model):
    name = models.CharField(max_length=150)
    normalized_name = models.CharField(max_length=180, blank=True, db_index=True)
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

    @staticmethod
    def normalize_name(value):
        return ' '.join((value or '').strip().lower().split())

    def save(self, *args, **kwargs):
        self.normalized_name = self.normalize_name(self.name)
        super().save(*args, **kwargs)

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
    DESIRED_MATCH_CHOICES = [
        ('exact_printing', 'Solo esta version'),
        ('any_printing', 'Busco cualquier version'),
    ]

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='user_cards')
    card = models.ForeignKey(Card, on_delete=models.CASCADE)
    is_owned = models.BooleanField(default=False)
    quantity_owned = models.PositiveIntegerField(default=0)
    quantity_required = models.PositiveIntegerField(default=0)
    desired_match_mode = models.CharField(max_length=20, choices=DESIRED_MATCH_CHOICES, default='exact_printing')
    listing_intent = models.CharField(max_length=16, choices=LISTING_INTENT_CHOICES, default='trade')
    condition = models.CharField(max_length=24, choices=CONDITION_CHOICES, default='near_mint')
    asking_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)

    def total_price(self):
        return self.card.price * self.quantity_owned if self.is_owned else 0

    def __str__(self):
        status = 'Poseida' if self.is_owned else 'Deseada'
        return f"{self.user.username} - {self.card.name} ({status})"

    class Meta:
        indexes = [
            models.Index(fields=['user', 'is_owned'], name='users_userc_user_id_403432_idx'),
            models.Index(fields=['is_owned', 'card'], name='users_userc_is_owne_202875_idx'),
            models.Index(fields=['is_owned', 'desired_match_mode'], name='users_userc_is_owne_024d3f_idx'),
            models.Index(fields=['listing_intent', 'is_owned'], name='users_userc_listing_eb98ca_idx'),
        ]


class Notification(models.Model):
    CATEGORY_CHOICES = [
        ('system', 'Sistema'),
        ('match', 'Match'),
        ('offer', 'Oferta'),
        ('trade', 'Intercambio'),
    ]
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_notifications')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_notifications')
    title = models.CharField(max_length=120, blank=True, null=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='system')
    action_url = models.CharField(max_length=255, blank=True, null=True)
    TYPE_CHOICES = [
        ('info', 'Informativo'),
        ('action', 'Accion'),
        ('error', 'Error'),
    ]
    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default='info')

    def __str__(self):
        return f"Notificacion de {self.sender.username} para {self.receiver.username}: {self.message}"


class Exchange(models.Model):
    PURCHASE_MODE_CHOICES = [
        ('buy_now', 'Buy Now'),
        ('offer', 'Offer'),
    ]
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_exchanges')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_exchanges')
    listing = models.ForeignKey(
        UserCard,
        on_delete=models.SET_NULL,
        related_name='sale_exchanges',
        blank=True,
        null=True,
    )
    target_card = models.ForeignKey(
        Card,
        on_delete=models.SET_NULL,
        related_name='targeted_exchanges',
        blank=True,
        null=True,
    )
    sender_cards = models.TextField()
    receiver_cards = models.TextField()
    date = models.DateTimeField(auto_now_add=True)
    STATUS_CHOICES = [
        ('pending', 'Pendiente'),
        ('accepted', 'Aceptado'),
        ('completed', 'Finalizado'),
        ('rejected', 'Rechazado'),
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    EXCHANGE_TYPE_CHOICES = [
        ('sale', 'Venta'),
        ('trade', 'Cambio'),
    ]
    exchange_type = models.CharField(max_length=10, choices=EXCHANGE_TYPE_CHOICES, default='trade')
    purchase_mode = models.CharField(max_length=16, choices=PURCHASE_MODE_CHOICES, default='offer')
    offer_amount = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    agreed_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    inventory_settled = models.BooleanField(default=False)
    message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"Intercambio entre {self.sender.username} y {self.receiver.username} el {self.date}"

    class Meta:
        indexes = [
            models.Index(fields=['exchange_type', 'status'], name='users_excha_exchang_113997_idx'),
            models.Index(fields=['target_card', 'status'], name='users_excha_target__ec1dcf_idx'),
            models.Index(fields=['listing', 'status'], name='users_excha_listing_034e31_idx'),
        ]


class TradeOffer(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pendiente'),
        ('accepted', 'Aceptado'),
        ('completed', 'Finalizado'),
        ('rejected', 'Rechazado'),
    ]

    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_trade_offers')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_trade_offers')
    requested_listing = models.ForeignKey(UserCard, on_delete=models.CASCADE, related_name='trade_requests')
    requested_listing_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    offered_cards_value = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    cash_due = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    inventory_settled = models.BooleanField(default=False)
    message = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Oferta de {self.sender.username} para {self.receiver.username} por {self.requested_listing.card.name}"


class TradeOfferItem(models.Model):
    trade_offer = models.ForeignKey(TradeOffer, on_delete=models.CASCADE, related_name='items')
    offered_user_card = models.ForeignKey(UserCard, on_delete=models.CASCADE, related_name='trade_offer_items')
    quantity = models.PositiveIntegerField(default=1)
    valuation_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    def __str__(self):
        return f"{self.offered_user_card.card.name} x{self.quantity}"


class MeetingAgreement(models.Model):
    exchange = models.OneToOneField(Exchange, on_delete=models.CASCADE, related_name='meeting_agreement', blank=True, null=True)
    trade_offer = models.OneToOneField(TradeOffer, on_delete=models.CASCADE, related_name='meeting_agreement', blank=True, null=True)
    store = models.ForeignKey(Store, on_delete=models.SET_NULL, related_name='meeting_agreements', blank=True, null=True)
    meeting_date = models.DateField(blank=True, null=True)
    meeting_time = models.TimeField(blank=True, null=True)
    seller_notes = models.TextField(blank=True, null=True)
    contact_shared_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        transaction = self.exchange or self.trade_offer
        return f"Meeting agreement #{self.id} for {transaction}"


class TransactionReview(models.Model):
    TAG_CHOICES = [
        ('punctual', 'Puntual'),
        ('condition_ok', 'Carta en buen estado'),
        ('good_communication', 'Buena comunicacion'),
        ('price_respected', 'Precio respetado'),
        ('friendly', 'Trato amable'),
    ]

    meeting = models.ForeignKey(MeetingAgreement, on_delete=models.CASCADE, related_name='reviews')
    reviewer = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='given_transaction_reviews')
    reviewed_user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_transaction_reviews')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    tags = models.CharField(max_length=180, blank=True, default='')
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('meeting', 'reviewer')]
        indexes = [
            models.Index(fields=['reviewed_user', 'created_at'], name='users_trans_reviewe_71f0d2_idx'),
            models.Index(fields=['meeting', 'reviewer'], name='users_trans_meeting_88d32c_idx'),
        ]

    def __str__(self):
        return f"{self.rating}/5 de {self.reviewer.username} para {self.reviewed_user.username}"
