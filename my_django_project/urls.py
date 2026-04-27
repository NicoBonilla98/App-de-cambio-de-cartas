from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.shortcuts import render
from users.models import Card, CustomUser, Exchange

def home(request):
    context = {
        'featured_cards': Card.objects.order_by('-price', 'name')[:3],
        'active_collectors': CustomUser.objects.count(),
        'listed_cards': Card.objects.count(),
        'active_exchanges': Exchange.objects.count(),
    }
    return render(request, 'home.html', context)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'),  # Redirige a la página principal
    path('users/', include('users.urls')),  # Incluye las URLs de la app users
    path('', home, name='home'),  # Ruta para la pantalla de inicio
]
