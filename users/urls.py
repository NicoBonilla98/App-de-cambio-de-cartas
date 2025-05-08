# filepath: /Users/usuario/Documents/Proyectos python/my_django_project/users/urls.py
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views
from .views import reject_offer, mark_all_resolved

urlpatterns = [
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('cards/', views.card_list, name='card_list'),
    path('add_card/<int:card_id>/<int:is_owned>/', views.add_card, name='add_card'),
    path('register/', views.register, name='register'),  # Ruta para el registro
    path('register_cards/', views.register_cards, name='register_cards'),  # Nueva ruta
    path('search_card/', views.search_card, name='search_card'),
    path('delete_card/<int:card_id>/', views.delete_card, name='delete_card'),
    path('edit_card_quantity/<int:card_id>/', views.edit_card_quantity, name='edit_card_quantity'),
    path('search_card_matches/', views.search_card_matches, name='search_card_matches'),
    path('edit_profile/', views.edit_user_profile, name='edit_profile'),
    path('search_users_with_desired_card/', views.search_users_with_desired_card, name='search_users_with_desired_card'),
    path('view_user_cards/', views.view_user_cards, name='view_user_cards'),
    path('send_trade_request/', views.send_trade_request, name='send_trade_request'),
    path('send_notification/', views.send_notification, name='send_notification'),
    path('notifications/', views.list_notifications, name='list_notifications'),
    path('accept_notification/', views.accept_notification, name='accept_notification'),
    path('reject_notification/', views.reject_notification, name='reject_notification'),
    path('reject_offer/', reject_offer, name='reject_offer'),
    path('mark_all_resolved/', mark_all_resolved, name='mark_all_resolved'),
    path('create_card/', views.create_card, name='create_card'),
    path('user_info/<int:user_id>/', views.view_user_info, name='view_user_info'),
    path('exchanges/', views.list_exchanges, name='list_exchanges'),
    path('make_purchase_offer/', views.make_purchase_offer, name='make_purchase_offer'),
    path('pending_transactions/', views.pending_transactions, name='pending_transactions'),
    path('accept_exchange/<int:exchange_id>/', views.accept_exchange, name='accept_exchange'),
    path('reject_exchange/<int:exchange_id>/', views.reject_exchange, name='reject_exchange'),
    path('upload_file/', views.upload_file, name='upload_file'),
    path('import_cards/', views.import_cards, name='import_cards'),
]