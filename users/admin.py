from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Card, UserCard, CustomUser, Exchange

admin.site.register(Card)
admin.site.register(UserCard)

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Información Adicional", {"fields": ("city",)}),
    )
    list_display = ("username", "email", "is_staff", "is_superuser", "city")
    list_filter = ("is_staff", "is_superuser", "city")
    search_fields = ("username", "email", "city")

@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = ('sender', 'receiver', 'date')
    list_filter = ('date',)
    search_fields = ('sender__username', 'receiver__username')
