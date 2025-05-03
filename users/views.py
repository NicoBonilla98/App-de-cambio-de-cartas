from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from .models import Card, UserCard, CustomUser, Notification, Exchange
from .forms import UserRegisterForm, CardForm
from django.contrib import messages
from django.db.models import Q
from django.contrib.auth.forms import UserChangeForm
from django import forms

@login_required
def card_list(request):
    owned_cards = UserCard.objects.filter(user=request.user, is_owned=True)
    desired_cards = UserCard.objects.filter(user=request.user, is_owned=False)

    # Calcular el valor total de la colección
    total_collection_value = sum(card.total_price() for card in owned_cards)

    return render(request, 'users/card_list.html', {
        'owned_cards': owned_cards,
        'desired_cards': desired_cards,
        'total_collection_value': total_collection_value,
    })

@login_required
def add_card(request, card_id, is_owned):
    card = get_object_or_404(Card, id=card_id)
    UserCard.objects.create(user=request.user, card=card, is_owned=bool(is_owned))
    return redirect('card_list')

@login_required
def delete_card(request, card_id):
    card = get_object_or_404(UserCard, id=card_id, user=request.user)
    card.delete()
    return redirect('card_list')

def register(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('username')
            messages.success(request, f'Cuenta creada para {username}. ¡Ahora puedes iniciar sesión!')
            return redirect('home')
    else:
        form = UserRegisterForm()
    return render(request, 'users/register.html', {'form': form})

@staff_member_required
def create_card(request):
    if request.method == 'POST':
        form = CardForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Carta creada exitosamente.')
            return redirect('create_card')
    else:
        form = CardForm()
    return render(request, 'users/create_card.html', {'form': form})

@login_required
def register_cards(request):
    if request.method == 'POST':
        card_id = request.POST.get('card_id')
        card = get_object_or_404(Card, id=card_id)

        is_owned = request.POST.get('card_type') == 'owned'
        quantity_owned = int(request.POST.get('quantity_owned', 0) or 0)
        quantity_required = int(request.POST.get('quantity_required', 0) or 0)

        UserCard.objects.create(
            user=request.user,
            card=card,
            is_owned=is_owned,
            quantity_owned=quantity_owned,
            quantity_required=quantity_required
        )

        messages.success(request, 'Carta registrada exitosamente.')
        return redirect('card_list')

    cards = Card.objects.all()
    return render(request, 'users/register_cards.html', {'cards': cards})

@login_required
def search_card(request):
    card_name = request.GET.get('card_name', '').strip()
    if card_name:
        matching_cards = UserCard.objects.filter(
            Q(card__name__icontains=card_name),
            ~Q(user=request.user),
            is_owned=True
        )
        return render(request, 'users/search_results.html', {'matching_cards': matching_cards})
    return render(request, 'users/search_results.html', {'matching_cards': []})

@login_required
def edit_card_quantity(request, card_id):
    if request.method == 'POST':
        new_quantity = request.POST.get('edit_card_quantity')

        try:
            user_card = UserCard.objects.get(user=request.user, card_id=card_id)
            if user_card.is_owned:
                user_card.quantity_owned = new_quantity
            else:
                user_card.quantity_required = new_quantity
            user_card.save()
            return redirect('card_list')
        except UserCard.DoesNotExist:
            return render(request, 'users/card_list.html', {
                'error_message': 'La carta no existe o no pertenece a tu colección.'
            })

    return redirect('card_list')

@login_required
def search_card_matches(request):
    card_name = request.GET.get('card_name', '').strip()
    if card_name:
        matching_cards = UserCard.objects.filter(
            card__name__iexact=card_name,
            is_owned=True
        ).exclude(user=request.user)
        return render(request, 'users/search_results.html', {
            'matching_cards': matching_cards,
            'searched_card': card_name
        })
    return render(request, 'users/search_results.html', {
        'matching_cards': [],
        'searched_card': None
    })

@login_required
def search_users_with_desired_card(request):
    card_name = request.GET.get('card_name', '').strip()
    if card_name:
        interested_users = UserCard.objects.filter(
            card__name__iexact=card_name,
            is_owned=False
        ).exclude(user=request.user)
        return render(request, 'users/search_results.html', {
            'matching_cards': interested_users,
            'searched_card': card_name
        })
    return render(request, 'users/search_results.html', {
        'matching_cards': [],
        'searched_card': None
    })

@login_required
def edit_user_profile(request):
    user_profile, created = CustomUser.objects.get_or_create(id=request.user.id)

    if request.user.id != user_profile.id:
        messages.error(request, 'No tienes permiso para editar este perfil.')
        return redirect('view_user_info', user_id=request.user.id)

    class EditProfileForm(forms.ModelForm):
        class Meta:
            model = CustomUser
            fields = ['phone_number', 'preferred_store', 'transaction_preference', 'city']

    if request.method == 'POST':
        form = EditProfileForm(request.POST, instance=user_profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Tu perfil ha sido actualizado correctamente.')
            return redirect('view_user_info', user_id=request.user.id)
    else:
        form = EditProfileForm(instance=user_profile)

    return render(request, 'users/edit_profile.html', {'form': form})

@login_required
def view_user_cards(request):
    user_id = request.GET.get('user_id')
    notification_id = request.GET.get('notification_id')

    if not user_id:
        messages.error(request, 'No se proporcionó un ID de usuario válido.')
        return redirect('list_notifications')

    try:
        selected_user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        messages.error(request, 'No se encontró un usuario con el ID proporcionado.')
        return redirect('list_notifications')

    user_cards = UserCard.objects.filter(user=selected_user, is_owned=True)

    # Obtener la carta deseada desde la notificación
    desired_card = None
    if notification_id:
        try:
            notification = Notification.objects.get(id=notification_id, receiver=request.user)
            desired_card = notification.message.split("'")[1]  # Extraer la carta deseada del mensaje
        except (Notification.DoesNotExist, IndexError):
            pass

    return render(request, 'users/view_user_cards.html', {
        'selected_user': selected_user,
        'user_cards': user_cards,
        'searched_card': desired_card,  # Pasar la carta deseada al template
        'show_price': True  # Indicate to the template to show prices
    })

@login_required
def send_trade_request(request):
    if request.method == 'POST':
        desired_card = request.POST.get('desired_card')
        selected_cards = request.POST.getlist('selected_cards')
        receiver_id = request.POST.get('user_id')

        if not selected_cards:
            messages.error(request, 'Seleccione una o más cartas a cambiar.')
            return redirect(f"/users/view_user_cards/?user_id={receiver_id}&notification_id={request.GET.get('notification_id')}")

        selected_cards_str = ', '.join(selected_cards)
        message = f"{request.user.username} ofrece '{desired_card}' por '{selected_cards_str}'."

        # Enviar notificación al usuario correspondiente
        receiver = get_object_or_404(CustomUser, id=receiver_id)
        Notification.objects.create(
            sender=request.user,
            receiver=receiver,
            message=message,
            type='exchange'  # Tipo de notificación: intercambio
        )

        # Marcar la notificación como resuelta
        if 'notification_id' in request.GET:
            notification_id = request.GET.get('notification_id')
            notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
            notification.type = 'resolved'
            notification.is_read = True
            notification.save()

        messages.success(request, 'Solicitud de intercambio enviada correctamente.')
        return redirect('list_notifications')

@login_required
def send_notification(request):
    if request.method == 'POST':
        card_name = request.POST.get('card_name')
        owner_id = request.POST.get('owner_id')
        if card_name and owner_id:
            owner = get_object_or_404(CustomUser, id=owner_id)
            message = f"{request.user.username} busca la carta '{card_name}', ¿quieres revisar sus cartas en posesión?"
            Notification.objects.create(
                sender=request.user,
                receiver=owner,
                message=message,
                type='action'  # Tipo de notificación: acción
            )
            messages.info(request, f"Notificación enviada a {owner.username}: {message}")

        return redirect('card_list')

@login_required
def list_notifications(request):
    notifications = Notification.objects.filter(receiver=request.user).exclude(type='resolved').order_by('-created_at')
    return render(request, 'users/notifications.html', {'notifications': notifications})

@login_required
def accept_notification(request):
    if request.method == 'POST':
        notification_id = request.POST.get('notification_id')
        notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)

        # Redirigir a view_user_info si la notificación es de tipo exchange y luego cambiar el estado
        if notification.type == 'exchange':
            response = redirect('view_user_info', user_id=notification.sender.id)
            notification.type = 'resolved'
            notification.is_read = True
            notification.save()
            return response

        # Cambiar el estado de la notificación solo para otros tipos
        notification.type = 'resolved'
        notification.is_read = True
        notification.save()

        # Redirigir a view_user_cards para otros tipos de notificaciones
        return redirect(f"/users/view_user_cards/?user_id={notification.sender.id}&notification_id={notification.id}")

@login_required
def reject_notification(request):
    if request.method == 'POST':
        notification_id = request.POST.get('notification_id')
        notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
        notification.is_read = True
        notification.type = 'resolved'  # Cambiar el tipo a resuelta
        notification.save()

        # Enviar notificación de rechazo al emisor
        message = f"{request.user.username} no aceptó el cambio."
        Notification.objects.create(
            sender=request.user,
            receiver=notification.sender,
            message=message,
            type='info'  # Tipo de notificación: informativo
        )

        return redirect('list_notifications')

@login_required
def reject_offer(request):
    if request.method == 'POST':
        # Cambiar la notificación aceptada a tipo resuelta
        if 'notification_id' in request.POST:
            notification_id = request.POST.get('notification_id')
            notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
            notification.type = 'resolved'
            notification.is_read = True
            notification.save()

        # Enviar notificación de rechazo al remitente
        message = f"{request.user.username} ha rechazado tu oferta."
        Notification.objects.create(
            sender=request.user,
            receiver=notification.sender,
            message=message,
            type='info'  # Tipo de notificación: informativo
        )

        return redirect('list_notifications')

@login_required
def mark_all_resolved(request):
    if request.method == 'POST':
        Notification.objects.filter(receiver=request.user).update(type='resolved', is_read=True)
        messages.success(request, 'Todas las notificaciones han sido marcadas como resueltas.')
        return redirect('list_notifications')

@login_required
def view_user_info(request, user_id):
    user = get_object_or_404(CustomUser, id=user_id)
    user_profile = get_object_or_404(CustomUser, id=user.id)
    return render(request, 'users/user_info.html', {
        'username': user.username,
        'city': user.city,
        'user': user,
        'user_profile': user_profile
    })

@login_required
def list_exchanges(request):
    exchanges = Exchange.objects.all().order_by('-date')
    return render(request, 'users/exchange_list.html', {'exchanges': exchanges})