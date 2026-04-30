from collections import Counter
import csv
from decimal import Decimal, InvalidOperation
import io
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .forms import CardForm, UploadFileForm, UserRegisterForm
from .models import Card, CustomUser, Exchange, Notification, UserCard

SCRYFALL_API_BASE = 'https://api.scryfall.com'
SCRYFALL_SEARCH_LIMIT = 8
SCRYFALL_MIN_INTERVAL_SECONDS = 0.35
SCRYFALL_TIMEOUT_SECONDS = 8
SCRYFALL_BULK_LOOKUP_INTERVAL_SECONDS = 0.26
SCRYFALL_HEADERS = {
    'User-Agent': 'MakiExchange/1.0 (local development contact: desktop-app)',
    'Accept': 'application/json;q=0.9,*/*;q=0.8',
}


def _client_rate_limit_key(request):
    if request.user.is_authenticated:
        return f"scryfall-search-user-{request.user.pk}"
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    client_ip = forwarded_for.split(',')[0].strip() or request.META.get('REMOTE_ADDR', 'anon')
    return f"scryfall-search-ip-{client_ip}"


def _enforce_scryfall_rate_limit(request):
    now = time.monotonic()
    cache_key = _client_rate_limit_key(request)
    previous = cache.get(cache_key)
    if previous is not None and now - previous < SCRYFALL_MIN_INTERVAL_SECONDS:
        return False
    cache.set(cache_key, now, timeout=60)
    return True


def _to_decimal(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_int(value, default=1):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _scryfall_request(path, params):
    query_string = urlencode(params)
    request = Request(f"{SCRYFALL_API_BASE}{path}?{query_string}", headers=SCRYFALL_HEADERS)
    with urlopen(request, timeout=SCRYFALL_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode('utf-8'))


def _normalize_scryfall_card(card_data):
    image_uris = card_data.get('image_uris') or {}
    card_faces = card_data.get('card_faces') or []
    if not image_uris and card_faces:
        image_uris = card_faces[0].get('image_uris') or {}

    return {
        'scryfall_id': card_data.get('id', ''),
        'name': card_data.get('name', ''),
        'set_name': card_data.get('set_name', ''),
        'set_code': (card_data.get('set') or '').upper(),
        'collector_number': card_data.get('collector_number', ''),
        'rarity': (card_data.get('rarity') or '').replace('_', ' ').title(),
        'image_url': image_uris.get('normal') or image_uris.get('large') or '',
        'usd_price': card_data.get('prices', {}).get('usd'),
        'usd_foil_price': card_data.get('prices', {}).get('usd_foil'),
        'eur_price': card_data.get('prices', {}).get('eur'),
        'description': card_data.get('oracle_text') or '',
        'type_line': card_data.get('type_line') or '',
    }


def _upsert_card_from_payload(card_payload, asking_price=None):
    scryfall_id = (card_payload.get('scryfall_id') or '').strip()
    card_name = (card_payload.get('name') or '').strip()
    set_name = (card_payload.get('set_name') or '').strip()
    set_code = (card_payload.get('set_code') or '').strip().upper()
    collector_number = (card_payload.get('collector_number') or '').strip()
    image_url = (card_payload.get('image_url') or '').strip()
    description = (card_payload.get('description') or '').strip()
    rarity = (card_payload.get('rarity') or '').strip()
    usd_price = _to_decimal(card_payload.get('usd_price'))
    usd_foil_price = _to_decimal(card_payload.get('usd_foil_price'))
    eur_price = _to_decimal(card_payload.get('eur_price'))
    base_price = usd_price or _to_decimal(asking_price) or Decimal('0.00')

    if scryfall_id:
        card, _ = Card.objects.get_or_create(
            scryfall_id=scryfall_id,
            defaults={
                'name': card_name,
                'set_name': set_name,
                'set_code': set_code,
                'collector_number': collector_number,
                'image_url': image_url,
                'description': description,
                'rarity': rarity,
                'price': base_price,
                'usd_price': usd_price,
                'usd_foil_price': usd_foil_price,
                'eur_price': eur_price,
            },
        )
        updates = []
        for field, value in {
            'name': card_name,
            'set_name': set_name,
            'set_code': set_code,
            'collector_number': collector_number,
            'image_url': image_url,
            'description': description,
            'rarity': rarity,
            'price': base_price or card.price,
            'usd_price': usd_price,
            'usd_foil_price': usd_foil_price,
            'eur_price': eur_price,
        }.items():
            if getattr(card, field) != value and value not in (None, ''):
                setattr(card, field, value)
                updates.append(field)
        if updates:
            card.save(update_fields=updates)
        return card

    card, _ = Card.objects.get_or_create(
        name=card_name,
        set_code=set_code,
        defaults={
            'set_name': set_name,
            'collector_number': collector_number,
            'image_url': image_url,
            'description': description,
            'rarity': rarity,
            'price': base_price,
            'usd_price': usd_price,
            'usd_foil_price': usd_foil_price,
            'eur_price': eur_price,
        },
    )
    return card


def _normalize_csv_header(value):
    return ''.join(ch.lower() for ch in (value or '') if ch.isalnum())


def _normalize_set_text(value):
    tokens = []
    for token in ''.join(ch.lower() if ch.isalnum() else ' ' for ch in (value or '')).split():
        if token not in {'edition', 'the'}:
            tokens.append(token)
    return ' '.join(tokens)


def _is_header_row(row):
    normalized = [_normalize_csv_header(cell) for cell in row]
    header_tokens = {'count', 'qty', 'quantity', 'name', 'cardname', 'set', 'setname', 'edition', 'expansion'}
    return any(cell in header_tokens for cell in normalized)


def _extract_csv_field(row_dict, aliases):
    for key, value in row_dict.items():
        if _normalize_csv_header(key) in aliases:
            return (value or '').strip()
    return ''


def _parse_moxfield_csv(uploaded_file):
    raw_bytes = uploaded_file.read()
    decoded = raw_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.reader(io.StringIO(decoded))
    rows = list(reader)
    if not rows:
        return []

    data_rows = rows[1:] if _is_header_row(rows[0]) else rows
    parsed_rows = []
    for index, row in enumerate(data_rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        padded = row + [''] * max(0, 6 - len(row))
        row_dict = {
            'col0': padded[0],
            'col1': padded[1],
            'col2': padded[2],
            'col3': padded[3],
            'col4': padded[4],
            'col5': padded[5],
        }
        quantity = padded[0].strip()
        name = padded[1].strip()
        set_name = padded[2].strip()

        if _is_header_row(rows[0]):
            header = rows[0]
            row_dict = {header[i]: padded[i] for i in range(min(len(header), len(padded)))}
            quantity = _extract_csv_field(row_dict, {'count', 'qty', 'quantity', 'collected'})
            name = _extract_csv_field(row_dict, {'name', 'cardname', 'card'})
            set_name = _extract_csv_field(row_dict, {'edition', 'set', 'setname', 'expansion'})

        parsed_rows.append({
            'row_number': index,
            'quantity': _to_int(quantity, default=1),
            'card_name': name,
            'set_name': set_name,
        })
    return parsed_rows


def _bulk_lookup_scryfall_cards(rows):
    enriched_rows = []
    errors = []

    for row in rows:
        name = (row.get('card_name') or '').strip()
        set_name = (row.get('set_name') or '').strip()
        if not name:
            errors.append(f"Fila {row['row_number']}: no tiene nombre de carta.")
            continue

        cache_key = f"scryfallbulk-{_normalize_csv_header(name)}-{_normalize_csv_header(set_name)}"
        cached_card = cache.get(cache_key)
        if cached_card is None:
            try:
                payload = _scryfall_request(
                    '/cards/search',
                    {
                        'q': f'!"{name}"',
                        'unique': 'prints',
                        'order': 'released',
                        'dir': 'desc',
                    },
                )
                matches = [_normalize_scryfall_card(card) for card in payload.get('data', [])[:24]]
                normalized_expected_set = _normalize_set_text(set_name)
                exact_match = next(
                    (
                        card for card in matches
                        if card.get('set_name', '').strip().lower() == set_name.lower()
                        or card.get('set_code', '').strip().lower() == set_name.lower()
                        or (
                            normalized_expected_set
                            and (
                                _normalize_set_text(card.get('set_name', '')) == normalized_expected_set
                                or normalized_expected_set in _normalize_set_text(card.get('set_name', ''))
                                or _normalize_set_text(card.get('set_name', '')) in normalized_expected_set
                            )
                        )
                    ),
                    None,
                )
                cached_card = exact_match or (matches[0] if matches else None)
                cache.set(cache_key, cached_card, timeout=60 * 60)
            except HTTPError as exc:
                errors.append(f"Fila {row['row_number']}: Scryfall devolvio {exc.code} para {name}.")
                cached_card = None
            except (URLError, TimeoutError, ValueError):
                errors.append(f"Fila {row['row_number']}: no se pudo consultar Scryfall para {name}.")
                cached_card = None
            time.sleep(SCRYFALL_BULK_LOOKUP_INTERVAL_SECONDS)

        enriched_row = {
            'row_number': row['row_number'],
            'quantity': row['quantity'],
            'card_name': name,
            'csv_set_name': set_name,
            'condition': 'near_mint',
            'listing_intent': 'sell',
            'asking_price': '',
            'match_status': 'matched' if cached_card else 'missing',
        }
        if cached_card:
            enriched_row.update({
                'scryfall_id': cached_card.get('scryfall_id', ''),
                'set_name': cached_card.get('set_name', '') or set_name,
                'set_code': cached_card.get('set_code', ''),
                'collector_number': cached_card.get('collector_number', ''),
                'rarity': cached_card.get('rarity', ''),
                'image_url': cached_card.get('image_url', ''),
                'usd_price': cached_card.get('usd_price') or '',
                'usd_foil_price': cached_card.get('usd_foil_price') or '',
                'eur_price': cached_card.get('eur_price') or '',
                'description': cached_card.get('description', ''),
                'type_line': cached_card.get('type_line', ''),
                'asking_price': cached_card.get('usd_price') or '',
            })
        else:
            enriched_row.update({
                'scryfall_id': '',
                'set_name': set_name,
                'set_code': '',
                'collector_number': '',
                'rarity': '',
                'image_url': '',
                'usd_price': '',
                'usd_foil_price': '',
                'eur_price': '',
                'description': '',
                'type_line': '',
            })
        enriched_rows.append(enriched_row)

    return enriched_rows, errors

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
        scryfall_id = request.POST.get('scryfall_id', '').strip()
        card_name = request.POST.get('card_name', '').strip()
        set_name = request.POST.get('set_name', '').strip()
        set_code = request.POST.get('set_code', '').strip().upper()
        collector_number = request.POST.get('collector_number', '').strip()
        image_url = request.POST.get('image_url', '').strip()
        description = request.POST.get('description', '').strip()
        rarity = request.POST.get('rarity', '').strip()
        card_type = request.POST.get('card_type', 'owned')
        listing_intent = request.POST.get('listing_intent', 'trade')
        condition = request.POST.get('condition', 'near_mint')
        quantity = int(request.POST.get('quantity', 1) or 1)
        asking_price = _to_decimal(request.POST.get('asking_price'))
        usd_price = _to_decimal(request.POST.get('usd_price'))
        usd_foil_price = _to_decimal(request.POST.get('usd_foil_price'))
        eur_price = _to_decimal(request.POST.get('eur_price'))

        if not card_name or not set_name:
            messages.error(request, 'Selecciona una carta y su expansion antes de guardar.')
            return redirect('register_cards')

        card = _upsert_card_from_payload(
            {
                'scryfall_id': scryfall_id,
                'name': card_name,
                'set_name': set_name,
                'set_code': set_code,
                'collector_number': collector_number,
                'image_url': image_url,
                'description': description,
                'rarity': rarity,
                'usd_price': usd_price,
                'usd_foil_price': usd_foil_price,
                'eur_price': eur_price,
            },
            asking_price=asking_price,
        )

        is_owned = card_type == 'owned'
        UserCard.objects.create(
            user=request.user,
            card=card,
            is_owned=is_owned,
            quantity_owned=quantity if is_owned else 0,
            quantity_required=0 if is_owned else quantity,
            listing_intent=listing_intent,
            condition=condition,
            asking_price=asking_price,
        )

        messages.success(request, 'Carta registrada exitosamente.')
        return redirect('card_list')

    return render(request, 'users/register_cards.html')


@login_required
@require_GET
def scryfall_card_search(request):
    query = request.GET.get('q', '').strip()
    if len(query) < 3:
        return JsonResponse({'results': []})

    if not _enforce_scryfall_rate_limit(request):
        return JsonResponse(
            {
                'results': [],
                'error': 'Estás consultando demasiado rápido. Espera un momento antes de buscar otra vez.',
            },
            status=429,
        )

    try:
        payload = _scryfall_request(
            '/cards/search',
            {
                'q': query,
                'unique': 'prints',
                'order': 'released',
                'dir': 'desc',
            },
        )
    except HTTPError as exc:
        if exc.code == 404:
            return JsonResponse({'results': []})
        return JsonResponse(
            {'results': [], 'error': 'Scryfall no pudo responder la búsqueda en este momento.'},
            status=exc.code,
        )
    except (URLError, TimeoutError, ValueError):
        return JsonResponse(
            {'results': [], 'error': 'No fue posible conectar con Scryfall ahora mismo.'},
            status=502,
        )

    results = [_normalize_scryfall_card(card) for card in payload.get('data', [])[:SCRYFALL_SEARCH_LIMIT]]
    return JsonResponse({'results': results})

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
    context = {
        'form': UploadFileForm(),
        'bulk_rows': [],
        'parse_errors': [],
        'matched_count': 0,
        'missing_count': 0,
    }

    if request.method == 'POST':
        if request.POST.get('action') == 'publish':
            try:
                bulk_rows = json.loads(request.POST.get('bulk_payload', '[]'))
            except json.JSONDecodeError:
                messages.error(request, 'No se pudo leer el lote enviado.')
                return redirect('upload_file')

            created_count = 0
            for row in bulk_rows:
                if row.get('match_status') != 'matched':
                    continue

                card = _upsert_card_from_payload(row, asking_price=row.get('asking_price'))
                quantity = _to_int(row.get('quantity'), default=1)
                card_type = row.get('card_type', 'owned')
                is_owned = card_type == 'owned'
                UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=is_owned,
                    quantity_owned=quantity if is_owned else 0,
                    quantity_required=0 if is_owned else quantity,
                    listing_intent=row.get('listing_intent', 'sell'),
                    condition=row.get('condition', 'near_mint'),
                    asking_price=_to_decimal(row.get('asking_price')),
                )
                created_count += 1

            messages.success(request, f'Se publicaron {created_count} cartas desde el lote.')
            return redirect('card_list')

        form = UploadFileForm(request.POST, request.FILES)
        context['form'] = form
        if form.is_valid():
            uploaded_file = request.FILES['file']
            parsed_rows = _parse_moxfield_csv(uploaded_file)
            bulk_rows, parse_errors = _bulk_lookup_scryfall_cards(parsed_rows)
            context.update({
                'bulk_rows': bulk_rows,
                'parse_errors': parse_errors,
                'matched_count': len([row for row in bulk_rows if row.get('match_status') == 'matched']),
                'missing_count': len([row for row in bulk_rows if row.get('match_status') != 'matched']),
            })
            if bulk_rows:
                messages.success(request, 'CSV importado. Revisa el lote antes de publicar.')
            else:
                messages.error(request, 'No se encontraron filas validas en el CSV.')

    return render(request, 'users/upload_file.html', context)

@login_required
def import_cards(request):
    if request.method == 'POST':
        try:
            extracted_data = json.loads(request.POST.get('extracted_data', '[]'))
            for data in extracted_data:
                UserCard.objects.create(
                    user=request.user,
                    card=Card.objects.get_or_create(name=data['nombre_carta'], set_code='')[0],
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
            data = json.loads(request.POST.get('extracted_data', '[]'))
            for card in data:
                UserCard.objects.create(
                    user=request.user,
                    card=Card.objects.get_or_create(name=card['nombre_carta'], set_code='')[0],
                    quantity_owned=int(card['cantidad']),
                    is_owned=True
                )
            return JsonResponse({'status': 'success', 'message': 'Cards added to owned list successfully.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})

@login_required
def create_user_cards_from_txt(request):
    if request.method == 'POST':
        try:
            extracted_data = request.POST.get('extracted_data', '[]')
            if not extracted_data.strip():
                return JsonResponse({'status': 'error', 'message': 'No data provided in extracted_data.'})

            data = json.loads(extracted_data)
            for card_data in data:
                card, created = Card.objects.get_or_create(name=card_data['nombre_carta'], set_code='')
                UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=False,
                    quantity_owned=0,
                    quantity_required=int(card_data['cantidad'])
                )
            return JsonResponse({'status': 'success', 'message': 'User cards created successfully.'})
        except json.JSONDecodeError as e:
            return JsonResponse({'status': 'error', 'message': f'JSON decode error: {str(e)}'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})
