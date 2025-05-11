from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from .models import Card, UserCard, CustomUser, Notification, Exchange
from .forms import UserRegisterForm, CardForm, UploadFileForm
from django.contrib import messages
from django.db.models import Q
from django.contrib.auth.forms import UserChangeForm
from django import forms
from collections import Counter
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
import requests
import logging
import time

logger = logging.getLogger(__name__)

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

@login_required
def delete_all_owned_cards(request):
    if request.method == 'POST':
        UserCard.objects.filter(user=request.user, is_owned=True).delete()
        messages.success(request, 'Todas las cartas poseídas han sido eliminadas.')
        return redirect('card_list')
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})

@login_required
def delete_all_desired_cards(request):
    if request.method == 'POST':
        UserCard.objects.filter(user=request.user, is_owned=False).delete()
        messages.success(request, 'Todas las cartas deseadas han sido eliminadas.')
        return redirect('card_list')
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})

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
    matching_cards = []
    if card_name:
        matching_cards = Card.objects.filter(name__icontains=card_name)
    return render(request, 'users/register_cards.html', {'matching_cards': matching_cards})

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

        # Buscar el intercambio existente
        exchange = Exchange.objects.filter(sender=receiver, receiver=request.user, status='pending').first()
        if not exchange:
            messages.error(request, 'No se encontró un intercambio pendiente para actualizar.')
            return redirect('list_notifications')

        # Actualizar los detalles del intercambio
        exchange.sender_cards = ', '.join(selected_cards)
        exchange.receiver_cards = desired_card
        exchange.save()

        messages.success(request, 'Intercambio actualizado correctamente.')

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

        if not card_name or not owner_id:
            messages.error(request, 'Faltan datos para enviar la notificación.')
            return redirect('card_list')

        try:
            owner = get_object_or_404(CustomUser, id=owner_id)
            exchange = Exchange.objects.create(
                sender=request.user,
                receiver=owner,
                sender_cards='',  # No hay cartas ofrecidas en este caso
                receiver_cards=card_name,
                status='pending',
                exchange_type='trade'
            )
            message = f"{request.user.username} busca la carta '{card_name}', ¿quieres revisar sus cartas en posesión? (ID de intercambio: {exchange.id})"
            Notification.objects.create(
                sender=request.user,
                receiver=owner,
                message=message,
                type='action'
            )
            messages.info(request, f"Notificación enviada a {owner.username}: {message}")
        except Exception as e:
            messages.error(request, f"Error al enviar la notificación: {str(e)}")
            return redirect('card_list')

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

        # Si la notificación es de tipo exchange, también ejecuta accept_exchange
        if notification.type == 'exchange':
            try:
                exchange = Exchange.objects.get(sender=notification.sender, receiver=request.user, status='pending')
                exchange.status = 'accepted'
                exchange.save()
            except Exchange.DoesNotExist:
                messages.error(request, 'No se encontró un intercambio pendiente asociado a esta notificación.')
                return redirect('list_notifications')

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
    user_exchanges = Exchange.objects.filter(Q(sender=request.user) | Q(receiver=request.user)).order_by('-date')
    return render(request, 'users/exchange_list.html', {'exchanges': user_exchanges})

@login_required
def make_purchase_offer(request):
    if request.method == 'POST':
        card_name = request.POST.get('card_name')
        owner_id = request.POST.get('owner_id')

        if card_name and owner_id:
            owner = get_object_or_404(CustomUser, id=owner_id)
            message = f"{request.user.username} está interesado en comprar: {card_name}."
            Notification.objects.create(
                sender=request.user,
                receiver=owner,
                message=message,
                type='compra'  # Tipo de notificación: compra
            )
            messages.success(request, f"Notificación enviada a {owner.username}: {message}")

        return redirect('card_list')

@login_required
def pending_transactions(request):
    pending_exchanges = Exchange.objects.filter(receiver=request.user, status='pending')
    return render(request, 'users/pending_transactions.html', {'pending_exchanges': pending_exchanges})

@login_required
def accept_exchange(request, exchange_id):
    exchange = get_object_or_404(Exchange, id=exchange_id, receiver=request.user, status='pending')
    exchange.status = 'accepted'
    exchange.save()
    messages.success(request, 'Intercambio aceptado exitosamente.')
    return redirect('pending_transactions')

@login_required
def reject_exchange(request, exchange_id):
    exchange = get_object_or_404(Exchange, id=exchange_id, receiver=request.user, status='pending')
    exchange.status = 'rejected'
    exchange.save()
    messages.success(request, 'Intercambio rechazado exitosamente.')
    return redirect('pending_transactions')

@login_required
def home(request):
    # Obtener todas las transacciones aceptadas
    accepted_exchanges = Exchange.objects.filter(status='accepted')

    # Contar las cartas más repetidas en las transacciones aceptadas
    all_cards = []
    for exchange in accepted_exchanges:
        all_cards.extend(exchange.sender_cards.split(', '))
        all_cards.extend(exchange.receiver_cards.split(', '))

    most_common_cards = Counter(all_cards).most_common(5)  # Obtener las 5 cartas más repetidas

    return render(request, 'home.html', {'most_common_cards': most_common_cards})

@login_required
def upload_file(request):
    if request.method == 'POST':
        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES['file']
            extracted_data = []
            for line in uploaded_file:
                line_content = line.decode('utf-8').strip()
                try:
                    cantidad, resto = line_content.split(' ', 1)
                    nombre_carta = resto[:resto.index('(')].strip()
                    edicion = resto[resto.index('(') + 1:resto.index(')')].strip()
                    numero_id_carta = resto[resto.index(')') + 1:].strip()

                    extracted_data.append({
                        'cantidad': cantidad,
                        'nombre_carta': nombre_carta,
                        'edicion': edicion,
                        'numero_id_carta': numero_id_carta
                    })
                except Exception as e:
                    messages.error(request, f"Error al procesar la línea: {line_content}. Detalles: {str(e)}")
                    continue

            # Crear UserCards directamente desde los datos extraídos
            for card_data in extracted_data:
                card, created = Card.objects.get_or_create(name=card_data['nombre_carta'])
                UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=False,
                    quantity_owned=0,
                    quantity_required=int(card_data['cantidad'])
                )

            messages.success(request, 'Archivo procesado y cartas creadas correctamente.')
            return render(request, 'users/upload_file.html', {'form': form, 'extracted_data': extracted_data})
    else:
        form = UploadFileForm()
    return render(request, 'users/upload_file.html', {'form': form})

@login_required
def import_cards(request):
    if request.method == 'POST':
        try:
            extracted_data = json.loads(request.POST.get('extracted_data', '[]'))
            for data in extracted_data:
                UserCard.objects.create(
                    user=request.user,
                    card=Card.objects.get_or_create(name=data['nombre_carta'])[0],
                    quantity_owned=int(data['cantidad']),
                    is_owned=True
                )
            messages.success(request, 'Cartas importadas exitosamente a tus cartas en posesión.')
        except Exception as e:
            messages.error(request, f'Error al importar las cartas: {str(e)}')
        return redirect('card_list')

@csrf_exempt
def add_to_owned_cards(request):
    if request.method == 'POST':
        try:
            extracted_data = json.loads(request.POST.get('extracted_data', '[]'))
            for card_data in extracted_data:
                card, created = Card.objects.get_or_create(name=card_data['nombre_carta'])
                UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=True,
                    quantity_owned=int(card_data['cantidad']),
                    quantity_required=0
                )
            return JsonResponse({'status': 'success', 'message': 'User cards added to owned list successfully.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})

@login_required
def import_card_to_desired_list(request):
    if request.method == 'POST':
        try:
            extracted_data = json.loads(request.POST.get('extracted_data', '[]'))
            for card_data in extracted_data:
                card, created = Card.objects.get_or_create(name=card_data['nombre_carta'])
                UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=False,
                    quantity_owned=0,
                    quantity_required=int(card_data['cantidad'])
                )
            return JsonResponse({'status': 'success', 'message': 'User cards created successfully.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})

@login_required
def consultar_carta(request):
    card_name = request.GET.get('card_name', '').strip()
    api_data = None

    if card_name:
        time.sleep(0.05)  # Add a delay of 50 milliseconds to respect the API rate limit
        response = requests.get(f"https://api.scryfall.com/cards/named?fuzzy={card_name}")
        if response.status_code == 200:
            api_data = response.json()
        else:
            # If no card is found, perform a broader search
            response = requests.get(f"https://api.scryfall.com/cards/search?q={card_name}")
            if response.status_code == 200:
                api_data = response.json()
            else:
                api_data = {'error': 'No se encontraron cartas con ese nombre en la API de Scryfall.'}

    return render(request, 'users/consultar_carta.html', {'api_data': api_data})
