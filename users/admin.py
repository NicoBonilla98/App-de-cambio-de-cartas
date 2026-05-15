from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Card, City, CustomUser, Exchange, MeetingAgreement, Store, TradeOffer, TransactionReview, UserCard


admin.site.register(MeetingAgreement)
admin.site.register(TransactionReview)


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ('name', 'set_name', 'collector_number', 'price')
    search_fields = ('name', 'normalized_name', 'set_name', 'set_code', 'collector_number')
    list_filter = ('set_name', 'rarity')


@admin.register(UserCard)
class UserCardAdmin(admin.ModelAdmin):
    list_display = ('user', 'card', 'is_owned', 'listing_intent', 'condition', 'asking_price')
    search_fields = ('user__username', 'card__name', 'card__normalized_name')
    list_filter = ('is_owned', 'listing_intent', 'condition')
    autocomplete_fields = ('user', 'card')


class StoreInline(admin.TabularInline):
    model = Store
    extra = 1
    fields = ('name', 'slug', 'address', 'is_active')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)
    inlines = [StoreInline]


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ('name', 'city', 'address', 'is_active')
    list_filter = ('city', 'is_active')
    search_fields = ('name', 'city__name', 'address')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Informacion Adicional', {'fields': ('city', 'phone_number', 'preferred_store', 'transaction_preference')}),
    )
    list_display = ('username', 'email', 'is_staff', 'is_superuser', 'city', 'phone_number', 'preferred_store', 'transaction_preference')
    list_filter = ('is_staff', 'is_superuser', 'city', 'preferred_store', 'transaction_preference')
    search_fields = ('username', 'email', 'city')


@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = ('sender', 'receiver', 'target_card', 'exchange_type', 'status', 'inventory_settled', 'agreed_price', 'date')
    list_filter = ('date', 'exchange_type', 'status', 'purchase_mode', 'inventory_settled')
    search_fields = ('sender__username', 'receiver__username', 'target_card__name', 'receiver_cards')
    autocomplete_fields = ('listing', 'target_card')


@admin.register(TradeOffer)
class TradeOfferAdmin(admin.ModelAdmin):
    list_display = ('sender', 'receiver', 'requested_listing', 'status', 'inventory_settled', 'cash_due', 'created_at')
    list_filter = ('status', 'inventory_settled', 'created_at')
    search_fields = ('sender__username', 'receiver__username', 'requested_listing__card__name')
    autocomplete_fields = ('requested_listing',)
