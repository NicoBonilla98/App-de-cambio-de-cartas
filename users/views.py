from collections import Counter
import csv
from datetime import date
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Avg, Count, DecimalField, ExpressionWrapper, F, Max, Min, Q, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import CardForm, UploadFileForm, UserRegisterForm
from .models import Card, City, CustomUser, Exchange, MeetingAgreement, Notification, Store, TradeOffer, TradeOfferItem, TransactionReview, UserCard

SCRYFALL_API_BASE = 'https://api.scryfall.com'
SCRYFALL_SEARCH_LIMIT = 8
SCRYFALL_MIN_INTERVAL_SECONDS = 0.35
SCRYFALL_TIMEOUT_SECONDS = 8
SCRYFALL_BULK_LOOKUP_INTERVAL_SECONDS = 0.26
SCRYFALL_GLOBAL_MIN_INTERVAL_SECONDS = 0.16
OFFER_REVIEW_WINDOW = timedelta(days=2)
SCRYFALL_HEADERS = {
    'User-Agent': 'MakiExchange/1.0 (local development contact: desktop-app)',
    'Accept': 'application/json;q=0.9,*/*;q=0.8',
}
_SCRYFALL_REQUEST_LOCK = threading.Lock()
_SCRYFALL_LAST_REQUEST_AT = 0.0


def _pagination_prefix(request):
    query_params = request.GET.copy()
    query_params.pop('page', None)
    querystring = query_params.urlencode()
    return f"{querystring}&" if querystring else ''


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


def _listing_value(user_card):
    return _to_decimal(user_card.asking_price) or _to_decimal(user_card.card.price) or Decimal('0.00')


def _format_money(value):
    amount = _to_decimal(value) or Decimal('0.00')
    return f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _month_buckets(count=6):
    today = date.today()
    buckets = []
    year = today.year
    month = today.month
    for offset in range(count - 1, -1, -1):
        bucket_month = month - offset
        bucket_year = year
        while bucket_month <= 0:
            bucket_month += 12
            bucket_year -= 1
        while bucket_month > 12:
            bucket_month -= 12
            bucket_year += 1
        buckets.append((bucket_year, bucket_month))
    return buckets


def _find_offer_listing_for_exchange(exchange):
    if exchange.listing_id:
        return exchange.listing
    listing_ref = (exchange.sender_cards or '').strip()
    if listing_ref.startswith('listing:'):
        listing_id = listing_ref.split(':', 1)[1]
        if listing_id.isdigit():
            listing = UserCard.objects.filter(id=int(listing_id), user=exchange.receiver, is_owned=True).select_related('card', 'user').first()
            if listing:
                return listing
    return UserCard.objects.filter(
        user=exchange.receiver,
        is_owned=True,
        card__name__iexact=exchange.receiver_cards,
    ).select_related('card', 'user').order_by('id').first()


def _exchange_target_name(exchange):
    if exchange.target_card_id:
        return exchange.target_card.name
    return exchange.receiver_cards or 'Carta acordada'


def _exchange_agreed_price(exchange, listing=None):
    if exchange.agreed_price is not None:
        return exchange.agreed_price
    if exchange.offer_amount is not None:
        return exchange.offer_amount
    return _listing_value(listing) if listing else Decimal('0.00')


def _desired_card_matches(offered_user_card, desired_user_card):
    if desired_user_card.desired_match_mode == 'any_printing':
        return offered_user_card.card.normalized_name == desired_user_card.card.normalized_name
    return offered_user_card.card_id == desired_user_card.card_id


def _same_card_for_desired(card, desired_user_card):
    if desired_user_card.desired_match_mode == 'any_printing':
        return card.normalized_name == desired_user_card.card.normalized_name
    return card.id == desired_user_card.card_id


def _same_card_for_owned(card, owned_user_card, desired_match_mode='exact_printing'):
    if desired_match_mode == 'any_printing':
        return card.normalized_name == owned_user_card.card.normalized_name
    return card.id == owned_user_card.card_id


def _create_alert_notification(sender, receiver, title, message, action_url):
    if sender == receiver:
        return False
    duplicate_exists = Notification.objects.filter(
        receiver=receiver,
        category='match',
        action_url=action_url,
        type='info',
        is_read=False,
    ).exists()
    if duplicate_exists:
        return False
    Notification.objects.create(
        sender=sender,
        receiver=receiver,
        title=title,
        message=message,
        category='match',
        action_url=action_url,
        type='info',
    )
    return True


def _trade_offer_action_url(trade_offer):
    return reverse('trade_offer_review', args=[trade_offer.id])


def _hydrate_notification_action_url(notification):
    if notification.action_url:
        return notification

    if notification.type == 'action':
        trade_offer = TradeOffer.objects.filter(
            sender=notification.sender,
            receiver=notification.receiver,
            status='pending',
        ).order_by('-created_at').first()
        if trade_offer:
            notification.title = notification.title or f"Oferta por {trade_offer.requested_listing.card.name}"
            notification.category = 'trade'
            notification.action_url = _trade_offer_action_url(trade_offer)
            notification.save(update_fields=['title', 'category', 'action_url'])
            return notification

        exchange = Exchange.objects.filter(
            sender=notification.sender,
            receiver=notification.receiver,
            status='pending',
        ).order_by('-date').first()
        if exchange and exchange.receiver_cards:
            notification.title = notification.title or f"Match solicitado: {exchange.receiver_cards}"
            notification.category = 'match'
            notification.action_url = f"{reverse('search_card_matches')}?{urlencode({'card_name': exchange.receiver_cards})}"
            notification.save(update_fields=['title', 'category', 'action_url'])

    return notification


def _create_match_alerts_for_user_card(user_card):
    if not user_card.card_id:
        return 0

    created_count = 0
    normalized_name = user_card.card.normalized_name or Card.normalize_name(user_card.card.name)
    if user_card.is_owned:
        desired_cards = UserCard.objects.filter(
            is_owned=False,
        ).exclude(user=user_card.user).filter(
            Q(desired_match_mode='exact_printing', card_id=user_card.card_id) |
            Q(desired_match_mode='any_printing', card__normalized_name=normalized_name)
        ).select_related('user', 'card')
        for desired_card in desired_cards:
            action_url = f"{reverse('search_card_matches')}?desired_id={desired_card.id}"
            mode_label = desired_card.get_desired_match_mode_display()
            if _create_alert_notification(
                sender=user_card.user,
                receiver=desired_card.user,
                title=f"Match encontrado: {user_card.card.name}",
                message=(
                    f"{user_card.user.username} agrego {user_card.card.name} a su coleccion. "
                    f"Coincide con tu busqueda ({mode_label})."
                ),
                action_url=action_url,
            ):
                created_count += 1
    else:
        owned_cards = UserCard.objects.filter(is_owned=True, quantity_owned__gt=0).exclude(user=user_card.user)
        if user_card.desired_match_mode == 'any_printing':
            owned_cards = owned_cards.filter(card__normalized_name=normalized_name)
        else:
            owned_cards = owned_cards.filter(card_id=user_card.card_id)
        owned_cards = owned_cards.select_related('user', 'card')
        query_url = f"{reverse('card_list')}?{urlencode({'q': user_card.card.name})}"
        for owned_card in owned_cards:
            if _create_alert_notification(
                sender=user_card.user,
                receiver=owned_card.user,
                title=f"Alguien busca {user_card.card.name}",
                message=(
                    f"{user_card.user.username} agrego {user_card.card.name} a su wishlist. "
                    "Tu copia podria servir para una venta o intercambio."
                ),
                action_url=query_url,
            ):
                created_count += 1
    return created_count


def _expire_stale_offers():
    cutoff = timezone.now() - OFFER_REVIEW_WINDOW

    stale_exchanges = list(
        Exchange.objects.filter(status='pending', date__lte=cutoff).select_related(
            'sender', 'receiver', 'target_card'
        )
    )
    for exchange in stale_exchanges:
        exchange.status = 'rejected'
        exchange.save(update_fields=['status'])
        Notification.objects.create(
            sender=exchange.receiver,
            receiver=exchange.sender,
            message=f"Your offer for {_exchange_target_name(exchange)} expired after 2 days without response.",
            type='info'
        )

    stale_trade_offers = list(
        TradeOffer.objects.filter(status='pending', created_at__lte=cutoff).select_related(
            'sender', 'receiver', 'requested_listing__card'
        )
    )
    for trade_offer in stale_trade_offers:
        trade_offer.status = 'rejected'
        trade_offer.save(update_fields=['status'])
        Notification.objects.create(
            sender=trade_offer.receiver,
            receiver=trade_offer.sender,
            message=f"Your trade offer for {trade_offer.requested_listing.card.name} expired after 2 days without response.",
            type='info'
        )

    return {
        'expired_exchanges': len(stale_exchanges),
        'expired_trade_offers': len(stale_trade_offers),
    }


def _scryfall_request(path, params):
    global _SCRYFALL_LAST_REQUEST_AT
    query_string = urlencode(params)
    request = Request(f"{SCRYFALL_API_BASE}{path}?{query_string}", headers=SCRYFALL_HEADERS)
    with _SCRYFALL_REQUEST_LOCK:
        elapsed = time.monotonic() - _SCRYFALL_LAST_REQUEST_AT
        if elapsed < SCRYFALL_GLOBAL_MIN_INTERVAL_SECONDS:
            time.sleep(SCRYFALL_GLOBAL_MIN_INTERVAL_SECONDS - elapsed)
        _SCRYFALL_LAST_REQUEST_AT = time.monotonic()
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
    card_name = (card_payload.get('name') or card_payload.get('card_name') or '').strip()
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
    reader = csv.reader(io.StringIO(decoded), delimiter='\t')
    rows = list(reader)
    if not rows:
        return []

    data_rows = rows[1:] if _is_header_row(rows[0]) else rows
    parsed_rows = []
    pattern = re.compile(r'^\s*(\d+)\s+(.+?)\s+\(([^)]+)\)\s+([A-Za-z0-9-]+)\s*$')
    for index, row in enumerate(data_rows, start=1):
        if not row or not any(cell.strip() for cell in row):
            continue
        row_text = '\t'.join(row).strip()
        quantity = ''
        name = ''
        set_name = ''
        collector_number = ''

        if _is_header_row(rows[0]):
            padded = row + [''] * max(0, len(rows[0]) - len(row))
            header = rows[0]
            row_dict = {header[i]: padded[i] for i in range(min(len(header), len(padded)))}
            quantity = _extract_csv_field(row_dict, {'count', 'qty', 'quantity', 'collected'})
            name = _extract_csv_field(row_dict, {'name', 'cardname', 'card'})
            set_name = _extract_csv_field(row_dict, {'edition', 'set', 'setname', 'expansion', 'setcode'})
            collector_number = _extract_csv_field(row_dict, {'number', 'collectornumber', 'collector'})
        else:
            match = pattern.match(row_text)
            if match:
                quantity, name, set_name, collector_number = match.groups()
            else:
                parts = [part.strip() for part in row if part.strip()]
                if len(parts) >= 4:
                    quantity, name, set_name, collector_number = parts[:4]
                    set_name = set_name.strip('()')
                else:
                    compact_match = pattern.match(row_text.replace('\t', ' '))
                    if compact_match:
                        quantity, name, set_name, collector_number = compact_match.groups()
                    else:
                        continue

        parsed_rows.append({
            'row_number': index,
            'quantity': _to_int(quantity, default=1),
            'card_name': name,
            'set_name': set_name,
            'collector_number': collector_number,
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
                        if (
                            row.get('collector_number')
                            and card.get('collector_number', '').strip().lower() == row.get('collector_number', '').strip().lower()
                            and (
                                card.get('set_name', '').strip().lower() == set_name.lower()
                                or card.get('set_code', '').strip().lower() == set_name.lower()
                            )
                        )
                        or card.get('set_name', '').strip().lower() == set_name.lower()
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
            'csv_collector_number': row.get('collector_number', ''),
            'condition': 'near_mint',
            'listing_intent': 'sell',
            'asking_price': '',
            'match_status': 'matched' if cached_card else 'missing',
        }
        if cached_card:
            enriched_row.update({
                'scryfall_id': cached_card.get('scryfall_id', ''),
                'name': cached_card.get('name', '') or name,
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
                'name': name,
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
@require_POST
def bulk_lookup_chunk(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON payload.'}, status=400)

    rows = payload.get('rows', [])
    if not isinstance(rows, list):
        return JsonResponse({'status': 'error', 'message': 'Rows must be a list.'}, status=400)

    rows = rows[:5]
    enriched_rows, errors = _bulk_lookup_scryfall_cards(rows)
    return JsonResponse({
        'status': 'success',
        'rows': enriched_rows,
        'errors': errors,
    })

@login_required
def card_list(request):
    owned_cards = UserCard.objects.filter(user=request.user, is_owned=True, quantity_owned__gt=0)
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
    user_card = UserCard.objects.create(user=request.user, card=card, is_owned=bool(is_owned))
    _create_match_alerts_for_user_card(user_card)
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
        desired_match_mode = request.POST.get('desired_match_mode', 'exact_printing')
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
        user_card = UserCard.objects.create(
            user=request.user,
            card=card,
            is_owned=is_owned,
            quantity_owned=quantity if is_owned else 0,
            quantity_required=0 if is_owned else quantity,
            desired_match_mode='exact_printing' if is_owned else desired_match_mode,
            listing_intent=listing_intent,
            condition=condition,
            asking_price=asking_price,
        )
        _create_match_alerts_for_user_card(user_card)

        messages.success(request, 'Carta registrada exitosamente.')
        return redirect('card_list')

    return render(request, 'users/register_cards.html', {
        'form_title': 'Forge New Archive Record',
        'form_intro': 'Busca una impresion exacta en Scryfall, revisa la expansion y publica tu carta con un precio base confiable.',
        'submit_label': 'Forge Listing',
    })


def _user_card_edit_payload(user_card):
    card = user_card.card
    quantity = user_card.quantity_owned if user_card.is_owned else user_card.quantity_required
    return {
        'card_name': card.name or '',
        'set_name': card.set_name or '',
        'set_code': card.set_code or '',
        'collector_number': card.collector_number or '',
        'card_type': 'owned' if user_card.is_owned else 'desired',
        'desired_match_mode': user_card.desired_match_mode,
        'quantity': quantity or 1,
        'listing_intent': user_card.listing_intent,
        'condition': user_card.condition,
        'asking_price': str(user_card.asking_price or ''),
        'description': card.description or '',
        'scryfall_id': card.scryfall_id or '',
        'image_url': card.image_url or '',
        'rarity': card.rarity or '',
        'usd_price': str(card.usd_price or ''),
        'usd_foil_price': str(card.usd_foil_price or ''),
        'eur_price': str(card.eur_price or ''),
    }


@login_required
def edit_user_card(request, user_card_id):
    user_card = get_object_or_404(
        UserCard.objects.select_related('card'),
        id=user_card_id,
        user=request.user,
    )

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'delete':
            user_card.delete()
            messages.success(request, 'Carta eliminada de tu coleccion.')
            return redirect('card_list')

        card_type = request.POST.get('card_type', 'owned')
        desired_match_mode = request.POST.get('desired_match_mode', 'exact_printing')
        listing_intent = request.POST.get('listing_intent', 'trade')
        condition = request.POST.get('condition', 'near_mint')
        quantity = max(_to_int(request.POST.get('quantity'), 1), 1)
        asking_price = _to_decimal(request.POST.get('asking_price'))

        if listing_intent not in dict(UserCard.LISTING_INTENT_CHOICES):
            listing_intent = 'trade'
        if condition not in dict(UserCard.CONDITION_CHOICES):
            condition = 'near_mint'
        if desired_match_mode not in dict(UserCard.DESIRED_MATCH_CHOICES):
            desired_match_mode = 'exact_printing'

        is_owned = card_type == 'owned'
        user_card.is_owned = is_owned
        user_card.quantity_owned = quantity if is_owned else 0
        user_card.quantity_required = 0 if is_owned else quantity
        user_card.desired_match_mode = 'exact_printing' if is_owned else desired_match_mode
        user_card.listing_intent = listing_intent
        user_card.condition = condition
        user_card.asking_price = asking_price
        user_card.save(update_fields=[
            'is_owned',
            'quantity_owned',
            'quantity_required',
            'desired_match_mode',
            'listing_intent',
            'condition',
            'asking_price',
        ])
        _create_match_alerts_for_user_card(user_card)

        messages.success(request, 'Carta actualizada correctamente.')
        return redirect('card_list')

    return render(request, 'users/register_cards.html', {
        'edit_mode': True,
        'user_card': user_card,
        'edit_card_data': _user_card_edit_payload(user_card),
        'form_title': 'Edit Archive Record',
        'form_intro': 'Ajusta precio, condicion, cantidad o intencion de intercambio para esta carta.',
        'submit_label': 'Save Changes',
    })


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
    desired_id = request.GET.get('desired_id', '').strip()
    card_name = request.GET.get('card_name', '').strip()
    if desired_id.isdigit():
        desired_card = UserCard.objects.filter(id=int(desired_id), user=request.user, is_owned=False).select_related('card').first()
        if desired_card:
            matching_cards = UserCard.objects.filter(
                is_owned=True,
                quantity_owned__gt=0,
            ).exclude(user=request.user).select_related('card', 'user')
            if desired_card.desired_match_mode == 'any_printing':
                matching_cards = matching_cards.filter(card__name__iexact=desired_card.card.name)
            else:
                matching_cards = matching_cards.filter(card_id=desired_card.card_id)
            matching_cards = matching_cards.order_by('asking_price', 'user__username')
            marketplace_listings = matching_cards.filter(listing_intent__in=['sell', 'sell_trade']).count()
            trade_proposals = matching_cards.filter(listing_intent__in=['trade', 'sell_trade']).count()
            prices = [_listing_value(card) for card in matching_cards]
            median_price = Decimal('0.00')
            if prices:
                sorted_prices = sorted(prices)
                median_price = sorted_prices[len(sorted_prices) // 2]
            return render(request, 'users/search_results.html', {
                'matching_cards': matching_cards,
                'searched_card': desired_card.card.name,
                'desired_card': desired_card,
                'holders_count': matching_cards.count(),
                'marketplace_listings': marketplace_listings,
                'trade_proposals': trade_proposals,
                'median_price': median_price,
            })
    if card_name:
        matching_cards = UserCard.objects.filter(
            card__name__iexact=card_name,
            is_owned=True,
            quantity_owned__gt=0,
        ).exclude(user=request.user).select_related('card', 'user').order_by('asking_price', 'user__username')
        marketplace_listings = matching_cards.filter(listing_intent__in=['sell', 'sell_trade']).count()
        trade_proposals = matching_cards.filter(listing_intent__in=['trade', 'sell_trade']).count()
        prices = [_listing_value(card) for card in matching_cards]
        median_price = Decimal('0.00')
        if prices:
            sorted_prices = sorted(prices)
            median_price = sorted_prices[len(sorted_prices) // 2]
        return render(request, 'users/search_results.html', {
            'matching_cards': matching_cards,
            'searched_card': card_name,
            'desired_card': None,
            'holders_count': matching_cards.count(),
            'marketplace_listings': marketplace_listings,
            'trade_proposals': trade_proposals,
            'median_price': median_price,
        })
    return render(request, 'users/search_results.html', {
        'matching_cards': [],
        'searched_card': None,
        'desired_card': None,
        'holders_count': 0,
        'marketplace_listings': 0,
        'trade_proposals': 0,
        'median_price': Decimal('0.00'),
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
def update_desired_match_mode(request, user_card_id):
    desired_card = get_object_or_404(UserCard, id=user_card_id, user=request.user, is_owned=False)
    if request.method == 'POST':
        desired_match_mode = request.POST.get('desired_match_mode', 'exact_printing')
        if desired_match_mode not in dict(UserCard.DESIRED_MATCH_CHOICES):
            desired_match_mode = 'exact_printing'
        desired_card.desired_match_mode = desired_match_mode
        desired_card.save(update_fields=['desired_match_mode'])
        messages.success(request, 'La preferencia de match se actualizo correctamente.')
    return redirect(request.POST.get('next') or 'card_list')

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
            labels = {
                'phone_number': 'Secure comms',
                'preferred_store': 'Preferred store',
                'transaction_preference': 'Preferred transfer',
                'city': 'City sanctum',
            }
            widgets = {
                'phone_number': forms.TextInput(attrs={'placeholder': 'Telefono o contacto'}),
                'preferred_store': forms.Select(),
                'transaction_preference': forms.Select(),
                'city': forms.Select(),
            }

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            city_choices = [(city.name, city.name) for city in City.objects.filter(is_active=True)]
            if self.instance and self.instance.city and self.instance.city not in dict(city_choices):
                city_choices.insert(0, (self.instance.city, self.instance.city))
            self.fields['city'].widget.choices = city_choices

            store_choices = [('', 'Not selected')]
            store_choices += [
                (store.slug, f"{store.name} - {store.city.name}")
                for store in Store.objects.filter(is_active=True).select_related('city')
            ]
            if self.instance and self.instance.preferred_store and self.instance.preferred_store not in dict(store_choices):
                store_choices.insert(1, (self.instance.preferred_store, self.instance.get_preferred_store_display()))
            self.fields['preferred_store'].widget.choices = store_choices

            for field in self.fields.values():
                field.widget.attrs.update({'class': 'profile-form__control'})

    if request.method == 'POST':
        form = EditProfileForm(request.POST, instance=user_profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Tu perfil ha sido actualizado correctamente.')
            return redirect('edit_profile')
    else:
        form = EditProfileForm(instance=user_profile)

    owned_cards = UserCard.objects.filter(user=request.user, is_owned=True, quantity_owned__gt=0).select_related('card')
    total_collection_value = sum(card.total_price() for card in owned_cards)
    active_sales = Exchange.objects.filter(
        Q(sender=request.user) | Q(receiver=request.user),
        status='pending',
    ).count()
    active_trades = TradeOffer.objects.filter(
        Q(sender=request.user) | Q(receiver=request.user),
        status='pending',
    ).count()
    recent_cards = owned_cards.order_by('-id')[:4]
    recent_trade_offers = TradeOffer.objects.filter(
        Q(sender=request.user) | Q(receiver=request.user)
    ).select_related('sender', 'receiver', 'requested_listing__card').order_by('-created_at')[:4]
    reputation = TransactionReview.objects.filter(reviewed_user=request.user).aggregate(
        average=Avg('rating'),
        total=Count('id'),
    )

    return render(request, 'users/edit_profile.html', {
        'form': form,
        'total_collection_value': total_collection_value,
        'active_engagements': active_sales + active_trades,
        'owned_count': sum(card.quantity_owned for card in owned_cards),
        'recent_cards': recent_cards,
        'recent_trade_offers': recent_trade_offers,
        'reputation_average': reputation['average'] or 0,
        'reputation_total': reputation['total'],
    })

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

    user_cards = UserCard.objects.filter(user=selected_user, is_owned=True, quantity_owned__gt=0)

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
            category='trade',
            type='action'
        )

        # Buscar el intercambio existente
        exchange = Exchange.objects.filter(sender=receiver, receiver=request.user, status='pending').first()
        if not exchange:
            messages.error(request, 'No se encontró un intercambio pendiente para actualizar.')
            return redirect('list_notifications')

        # Actualizar los detalles del intercambio
        exchange.sender_cards = ', '.join(selected_cards)
        exchange.receiver_cards = desired_card
        exchange.target_card = Card.objects.filter(name__iexact=desired_card).first()
        exchange.save(update_fields=['sender_cards', 'receiver_cards', 'target_card'])

        messages.success(request, 'Intercambio actualizado correctamente.')

        # Marcar la notificación como resuelta
        if 'notification_id' in request.GET:
            notification_id = request.GET.get('notification_id')
            notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
            notification.is_read = True
            notification.save(update_fields=['is_read'])

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
                target_card=Card.objects.filter(name__iexact=card_name).first(),
                sender_cards='',  # No hay cartas ofrecidas en este caso
                receiver_cards=card_name,
                status='pending',
                exchange_type='trade'
            )
            message = f"{request.user.username} busca la carta '{card_name}', ¿quieres revisar sus cartas en posesión? (ID de intercambio: {exchange.id})"
            Notification.objects.create(
                sender=request.user,
                receiver=owner,
                title=f"Match solicitado: {card_name}",
                message=message,
                category='match',
                action_url=f"{reverse('search_card_matches')}?{urlencode({'card_name': card_name})}",
                type='action'
            )
            messages.info(request, f"Notificación enviada a {owner.username}: {message}")
        except Exception as e:
            messages.error(request, f"Error al enviar la notificación: {str(e)}")
            return redirect('card_list')

        return redirect('card_list')

@login_required
def list_notifications(request):
    _expire_stale_offers()
    notifications = list(Notification.objects.filter(receiver=request.user, is_read=False).order_by('-created_at'))
    notifications = [_hydrate_notification_action_url(notification) for notification in notifications]
    return render(request, 'users/notifications.html', {
        'notifications': notifications,
        'unread_count': sum(1 for notification in notifications if not notification.is_read),
        'match_count': sum(1 for notification in notifications if notification.category == 'match'),
    })


@login_required
def resolve_notification(request, notification_id):
    notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
    if request.method == 'POST':
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    return redirect('list_notifications')

@login_required
def accept_notification(request):
    if request.method == 'POST':
        notification_id = request.POST.get('notification_id')
        notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
        notification = _hydrate_notification_action_url(notification)
        if notification.action_url:
            notification.is_read = True
            notification.save(update_fields=['is_read'])
            return redirect(notification.action_url)

        # Si la notificación es de tipo exchange, también ejecuta accept_exchange
        if notification.category == 'trade':
            try:
                exchange = Exchange.objects.get(sender=notification.sender, receiver=request.user, status='pending')
                exchange.status = 'accepted'
                exchange.save()
            except Exchange.DoesNotExist:
                messages.error(request, 'No se encontró un intercambio pendiente asociado a esta notificación.')
                return redirect('list_notifications')

        # Redirigir a view_user_info si la notificación es de tipo exchange y luego cambiar el estado
        if notification.category == 'trade':
            response = redirect('view_user_info', user_id=notification.sender.id)
            notification.is_read = True
            notification.save(update_fields=['is_read'])
            return response

        # Cambiar el estado de la notificación solo para otros tipos
        notification.is_read = True
        notification.save(update_fields=['is_read'])

        # Redirigir a view_user_cards para otros tipos de notificaciones
        return redirect(f"/users/view_user_cards/?user_id={notification.sender.id}&notification_id={notification.id}")

@login_required
def reject_notification(request):
    if request.method == 'POST':
        notification_id = request.POST.get('notification_id')
        notification = get_object_or_404(Notification, id=notification_id, receiver=request.user)
        notification.is_read = True
        notification.save(update_fields=['is_read'])

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
            notification.is_read = True
            notification.save(update_fields=['is_read'])

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
        Notification.objects.filter(receiver=request.user).update(is_read=True)
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
    _expire_stale_offers()
    user_exchanges = Exchange.objects.filter(Q(sender=request.user) | Q(receiver=request.user)).select_related(
        'sender', 'receiver', 'listing__card', 'target_card'
    ).order_by('-date')
    trade_offers = TradeOffer.objects.filter(
        Q(sender=request.user) | Q(receiver=request.user)
    ).select_related('sender', 'receiver', 'requested_listing__card').prefetch_related('items__offered_user_card__card').order_by('-created_at')
    return render(request, 'users/exchange_list.html', {
        'exchanges': user_exchanges,
        'trade_offers': trade_offers,
    })

@login_required
def make_purchase_offer(request):
    if request.method == 'POST':
        card_name = request.POST.get('card_name')
        owner_id = request.POST.get('owner_id')
        listing_id = request.POST.get('listing_id')
        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER')
        purchase_mode = request.POST.get('purchase_mode', 'offer')
        buyer_message = request.POST.get('message', '').strip()

        if card_name and owner_id:
            listing = None
            if listing_id:
                listing = get_object_or_404(
                    UserCard.objects.select_related('card', 'user'),
                    id=listing_id,
                    user_id=owner_id,
                    is_owned=True,
                    quantity_owned__gt=0,
                )
                card_name = listing.card.name
            owner = listing.user if listing else get_object_or_404(CustomUser, id=owner_id)
            if owner == request.user:
                messages.error(request, 'No puedes comprar tu propia carta.')
                return redirect(next_url or 'marketplace')

            listing_value = _listing_value(listing) if listing else Decimal('0.00')
            offer_amount = listing_value

            if purchase_mode == 'offer':
                if not listing or listing.listing_intent != 'sell_trade':
                    messages.error(request, 'Este listing no acepta ofertas negociables.')
                    return redirect(next_url or 'marketplace')
                if listing_value <= Decimal('0.00'):
                    messages.error(request, 'No se puede negociar una oferta para una carta sin precio publicado.')
                    return redirect(next_url or 'marketplace')

                offer_amount = _to_decimal(request.POST.get('offer_amount'))
                min_offer = (listing_value * Decimal('0.75')).quantize(Decimal('0.01'))
                max_offer = (listing_value * Decimal('3.00')).quantize(Decimal('0.01'))
                if offer_amount is None:
                    messages.error(request, 'Ingresa un valor valido para la oferta.')
                    return redirect(next_url or 'marketplace')
                if offer_amount < min_offer or offer_amount > max_offer:
                    messages.error(
                        request,
                        f'La oferta debe estar entre ${_format_money(min_offer)} y ${_format_money(max_offer)}.'
                    )
                    return redirect(next_url or 'marketplace')
                message = (
                    f"{request.user.username} ofrecio ${_format_money(offer_amount)} por {card_name}. "
                    f"Precio publicado: ${_format_money(listing_value)}."
                )
                success_message = f"Oferta enviada a {owner.username} para revision."
            else:
                purchase_mode = 'buy_now'
                if listing and listing.listing_intent not in ['sell', 'sell_trade']:
                    messages.error(request, 'Este listing no esta disponible para compra directa.')
                    return redirect(next_url or 'marketplace')
                message = (
                    f"{request.user.username} quiere comprar de inmediato: {card_name} "
                    f"por ${_format_money(offer_amount)}."
                )
                success_message = f"Solicitud de compra enviada a {owner.username}."

            exchange = Exchange.objects.create(
                sender=request.user,
                receiver=owner,
                listing=listing,
                target_card=listing.card if listing else Card.objects.filter(name__iexact=card_name).first(),
                sender_cards=f'listing:{listing_id}' if listing_id else '',
                receiver_cards=card_name,
                status='pending',
                exchange_type='sale',
                purchase_mode=purchase_mode,
                offer_amount=offer_amount,
                agreed_price=offer_amount,
                message=buyer_message,
            )
            Notification.objects.create(
                sender=request.user,
                receiver=owner,
                title='Compra directa' if purchase_mode == 'buy_now' else 'Nueva oferta de compra',
                message=message,
                category='offer',
                action_url=reverse('active_ritual_offers'),
                type='action'
            )
            messages.success(request, success_message)

        return redirect(next_url or 'marketplace')

@login_required
def pending_transactions(request):
    _expire_stale_offers()
    pending_exchanges = Exchange.objects.filter(receiver=request.user, status='pending').select_related(
        'sender', 'receiver', 'listing__card', 'target_card'
    )
    return render(request, 'users/pending_transactions.html', {'pending_exchanges': pending_exchanges})


@login_required
def active_ritual_offers(request):
    expired = _expire_stale_offers()
    sale_exchanges = Exchange.objects.filter(
        receiver=request.user,
        status='pending',
        exchange_type='sale',
    ).select_related('sender', 'receiver', 'listing__card', 'target_card').order_by('-date')

    trade_offers = TradeOffer.objects.filter(
        receiver=request.user,
        status='pending',
    ).select_related('sender', 'receiver', 'requested_listing__card').prefetch_related(
        'items__offered_user_card__card'
    ).order_by('-created_at')

    grouped = {}

    for exchange in sale_exchanges:
        listing = _find_offer_listing_for_exchange(exchange)
        if not listing:
            continue
        key = f"sale-{listing.id}"
        group = grouped.setdefault(key, {
            'listing': listing,
            'proposals': [],
        })
        group['proposals'].append({
            'kind': 'sale',
            'sender': exchange.sender,
            'title': exchange.sender.username,
            'store': exchange.sender.get_preferred_store_display() or 'No preferred store',
            'value_label': 'Buy now value' if exchange.purchase_mode == 'buy_now' else 'Offer value',
            'value_text': f"${_format_money(_exchange_agreed_price(exchange, listing))}",
            'note': exchange.message,
            'primary_label': 'Accept Ritual',
            'primary_url': ('accept_exchange', exchange.id),
            'secondary_label': None,
            'secondary_url': None,
            'reject_url': ('reject_exchange', exchange.id),
        })

    for trade_offer in trade_offers:
        listing = trade_offer.requested_listing
        key = f"trade-{listing.id}"
        group = grouped.setdefault(key, {
            'listing': listing,
            'proposals': [],
        })
        cards_count = trade_offer.items.count()
        group['proposals'].append({
            'kind': 'trade',
            'sender': trade_offer.sender,
            'title': trade_offer.sender.username,
            'store': trade_offer.sender.get_preferred_store_display() or 'No preferred store',
            'value_label': 'Offer value',
            'value_text': f"Trade proposal: {cards_count} card{'s' if cards_count != 1 else ''}",
            'primary_label': 'View Trade',
            'primary_url': ('trade_offer_review', trade_offer.id),
            'secondary_label': 'Accept Ritual',
            'secondary_url': ('accept_trade_offer', trade_offer.id),
            'reject_url': ('reject_trade_offer', trade_offer.id),
        })

    offer_groups = sorted(
        grouped.values(),
        key=lambda item: (
            item['listing'].card.name.lower() if item['listing'].card and item['listing'].card.name else '',
            item['listing'].id,
        )
    )

    return render(request, 'users/active_ritual_offers.html', {
        'offer_groups': offer_groups,
        'total_proposals': sum(len(group['proposals']) for group in offer_groups),
        'expired_count': expired['expired_exchanges'] + expired['expired_trade_offers'],
    })

@login_required
@require_POST
def accept_exchange(request, exchange_id):
    _expire_stale_offers()
    exchange = Exchange.objects.select_related('target_card').filter(id=exchange_id, receiver=request.user, status='pending').first()
    if not exchange:
        messages.error(request, 'Esta oferta ya no esta disponible. Puede haber sido respondida o caducada.')
        return redirect('active_ritual_offers')
    exchange.status = 'accepted'
    if exchange.agreed_price is None:
        exchange.agreed_price = exchange.offer_amount
        exchange.save(update_fields=['status', 'agreed_price'])
    else:
        exchange.save(update_fields=['status'])
    MeetingAgreement.objects.get_or_create(exchange=exchange)
    target_name = _exchange_target_name(exchange)
    Notification.objects.create(
        sender=request.user,
        receiver=exchange.sender,
        title=f"Compra aceptada: {target_name}",
        message=f"{request.user.username} accepted your offer for {target_name}.",
        category='offer',
        action_url=reverse('meeting_exchange', args=[exchange.id]),
        type='info'
    )
    messages.success(request, 'Intercambio aceptado exitosamente.')
    return redirect('meeting_exchange', exchange_id=exchange.id)

@login_required
@require_POST
def reject_exchange(request, exchange_id):
    _expire_stale_offers()
    exchange = Exchange.objects.select_related('target_card').filter(id=exchange_id, receiver=request.user, status='pending').first()
    if not exchange:
        messages.error(request, 'Esta oferta ya no esta disponible. Puede haber sido respondida o caducada.')
        return redirect('active_ritual_offers')
    exchange.status = 'rejected'
    exchange.save()
    Notification.objects.create(
        sender=request.user,
        receiver=exchange.sender,
        message=f"{request.user.username} rejected your offer for {_exchange_target_name(exchange)}.",
        type='info'
    )
    messages.success(request, 'Intercambio rechazado exitosamente.')
    return redirect('active_ritual_offers')


def _meeting_store_options(seller):
    stores = Store.objects.filter(is_active=True).select_related('city')
    if seller.city:
        city_stores = stores.filter(city__name__iexact=seller.city)
        if city_stores.exists():
            return city_stores
    return stores


def _meeting_context_for_exchange(exchange):
    listing = _find_offer_listing_for_exchange(exchange)
    target_card = exchange.target_card or (listing.card if listing else None)
    agreed_price = _exchange_agreed_price(exchange, listing)
    return {
        'transaction_kind': 'sale',
        'seller': exchange.receiver,
        'buyer': exchange.sender,
        'listing': listing,
        'target_card': target_card,
        'target_name': target_card.name if target_card else exchange.receiver_cards or 'Carta acordada',
        'agreed_price': agreed_price,
        'card_discount': Decimal('0.00'),
        'offered_items': [],
        'transaction': exchange,
    }


def _meeting_context_for_trade_offer(trade_offer):
    return {
        'transaction_kind': 'trade',
        'seller': trade_offer.receiver,
        'buyer': trade_offer.sender,
        'listing': trade_offer.requested_listing,
        'target_card': trade_offer.requested_listing.card,
        'target_name': trade_offer.requested_listing.card.name,
        'agreed_price': trade_offer.cash_due,
        'card_discount': trade_offer.offered_cards_value,
        'offered_items': trade_offer.items.select_related('offered_user_card__card'),
        'transaction': trade_offer,
    }


def _settle_sale_inventory(exchange):
    locked_exchange = Exchange.objects.select_for_update().select_related('listing__card').get(pk=exchange.pk)
    if locked_exchange.inventory_settled:
        return locked_exchange, None

    listing = locked_exchange.listing or _find_offer_listing_for_exchange(locked_exchange)
    if not listing:
        return locked_exchange, 'No se encontro el listing asociado a esta venta.'

    listing = UserCard.objects.select_for_update().select_related('card').get(pk=listing.pk)
    if listing.quantity_owned < 1:
        return locked_exchange, f"No queda inventario disponible de {listing.card.name} para cerrar esta venta."

    listing.quantity_owned -= 1
    listing.save(update_fields=['quantity_owned'])

    locked_exchange.status = 'completed'
    locked_exchange.inventory_settled = True
    locked_exchange.save(update_fields=['status', 'inventory_settled'])
    return locked_exchange, None


def _settle_trade_inventory(trade_offer):
    locked_offer = TradeOffer.objects.select_for_update().select_related(
        'requested_listing__card'
    ).prefetch_related(
        'items__offered_user_card__card'
    ).get(pk=trade_offer.pk)
    if locked_offer.inventory_settled:
        return locked_offer, None

    required_quantities = {locked_offer.requested_listing_id: 1}
    for item in locked_offer.items.all():
        required_quantities[item.offered_user_card_id] = (
            required_quantities.get(item.offered_user_card_id, 0) + max(item.quantity, 1)
        )

    locked_cards = UserCard.objects.select_for_update().select_related('card').in_bulk(required_quantities.keys())
    for user_card_id, required_quantity in required_quantities.items():
        user_card = locked_cards.get(user_card_id)
        if not user_card:
            return locked_offer, 'Una de las cartas del acuerdo ya no existe en el inventario.'
        if user_card.quantity_owned < required_quantity:
            return locked_offer, (
                f"No queda inventario suficiente de {user_card.card.name}. "
                f"Disponible: {user_card.quantity_owned}, requerido: {required_quantity}."
            )

    for user_card_id, required_quantity in required_quantities.items():
        user_card = locked_cards[user_card_id]
        user_card.quantity_owned -= required_quantity
        user_card.save(update_fields=['quantity_owned'])

    locked_offer.status = 'completed'
    locked_offer.inventory_settled = True
    locked_offer.save(update_fields=['status', 'inventory_settled', 'updated_at'])
    return locked_offer, None


def _settle_inventory_for_transaction(transaction_object, transaction_kind):
    if transaction_kind == 'sale':
        return _settle_sale_inventory(transaction_object)
    if transaction_kind == 'trade':
        return _settle_trade_inventory(transaction_object)
    return transaction_object, None


def _complete_meeting_review(request, meeting, context):
    seller = context['seller']
    buyer = context['buyer']
    if request.user.id not in [seller.id, buyer.id]:
        messages.error(request, 'No tienes permiso para cerrar este acuerdo.')
        return redirect('marketplace')

    reviewed_user = buyer if request.user.id == seller.id else seller
    try:
        rating = int(request.POST.get('rating', '0'))
    except ValueError:
        rating = 0
    if rating < 1 or rating > 5:
        messages.error(request, 'Selecciona una calificacion entre 1 y 5.')
        return redirect(request.path)

    allowed_tags = {value for value, _label in TransactionReview.TAG_CHOICES}
    tags = [tag for tag in request.POST.getlist('tags') if tag in allowed_tags]
    comment = request.POST.get('comment', '').strip()

    with db_transaction.atomic():
        transaction_object, inventory_error = _settle_inventory_for_transaction(
            context['transaction'],
            context.get('transaction_kind'),
        )
        if inventory_error:
            messages.error(request, inventory_error)
            return redirect(request.path)

        context['transaction'] = transaction_object
        TransactionReview.objects.update_or_create(
            meeting=meeting,
            reviewer=request.user,
            defaults={
                'reviewed_user': reviewed_user,
                'rating': rating,
                'tags': ','.join(tags),
                'comment': comment,
            },
        )

    Notification.objects.create(
        sender=request.user,
        receiver=reviewed_user,
        title='Compra marcada como finalizada',
        message=(
            f"{request.user.username} marco como finalizado el acuerdo por "
            f"{context.get('target_name', 'la carta acordada')} y dejo una calificacion."
        ),
        category='offer' if context.get('transaction_kind') == 'sale' else 'trade',
        action_url=request.path,
        type='info',
    )
    messages.success(request, 'Compra finalizada y calificacion registrada.')
    return redirect(request.path)


def _meeting_response(request, meeting, context):
    seller = context['seller']
    buyer = context['buyer']
    read_only = request.GET.get('readonly') == '1'
    can_update_meeting = request.user.id == seller.id and not read_only
    if request.user.id not in [seller.id, buyer.id]:
        messages.error(request, 'No tienes permiso para revisar este acuerdo.')
        return redirect('marketplace')

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'complete':
            return _complete_meeting_review(request, meeting, context)

        if not can_update_meeting:
            messages.error(request, 'Solo el vendedor puede actualizar los detalles del encuentro.')
            return redirect(request.path)

        if action == 'share_contact':
            contact = seller.phone_number or seller.email or 'Contacto no configurado'
            store_label = meeting.store.name if meeting.store else 'tienda pendiente'
            target_name = context.get('target_name') or 'la carta acordada'
            when_label = ''
            if meeting.meeting_date:
                when_label = f" Fecha: {meeting.meeting_date}"
                if meeting.meeting_time:
                    when_label += f" {meeting.meeting_time.strftime('%H:%M')}"
            Notification.objects.create(
                sender=seller,
                receiver=buyer,
                title='Contacto del vendedor disponible',
                message=(
                    f"{seller.username} compartio su contacto para cerrar {target_name}: "
                    f"{contact}. Punto de encuentro: {store_label}.{when_label}"
                ),
                category='offer',
                action_url=request.path,
                type='info',
            )
            meeting.contact_shared_at = timezone.now()
            meeting.save(update_fields=['contact_shared_at', 'updated_at'])
            messages.success(request, 'Contacto enviado al comprador.')
            return redirect(request.path)

        store_id = request.POST.get('store')
        meeting.store = Store.objects.filter(id=store_id, is_active=True).first() if store_id else None
        meeting.meeting_date = request.POST.get('meeting_date') or None
        meeting.meeting_time = request.POST.get('meeting_time') or None
        meeting.seller_notes = request.POST.get('seller_notes', '').strip()
        meeting.save()
        messages.success(request, 'Detalles del encuentro actualizados.')
        return redirect(request.path)

    reviews = list(meeting.reviews.select_related('reviewer', 'reviewed_user').order_by('created_at'))
    current_review = next((review for review in reviews if review.reviewer_id == request.user.id), None)
    counterpart_review = next((review for review in reviews if review.reviewer_id != request.user.id), None)
    is_completed = getattr(context['transaction'], 'status', '') == 'completed'
    context.update({
        'meeting': meeting,
        'store_options': _meeting_store_options(seller),
        'is_seller': request.user.id == seller.id,
        'read_only': read_only,
        'can_update_meeting': can_update_meeting,
        'review_tags': TransactionReview.TAG_CHOICES,
        'current_review': current_review,
        'counterpart_review': counterpart_review,
        'reviews': reviews,
        'is_completed': is_completed,
    })
    return render(request, 'users/meeting_agreement.html', context)


@login_required
def meeting_exchange(request, exchange_id):
    exchange = get_object_or_404(
        Exchange.objects.select_related('sender', 'receiver', 'listing__card', 'target_card'),
        id=exchange_id,
        status__in=['accepted', 'completed'],
        exchange_type='sale',
    )
    meeting, _ = MeetingAgreement.objects.get_or_create(exchange=exchange)
    return _meeting_response(request, meeting, _meeting_context_for_exchange(exchange))


@login_required
def meeting_trade_offer(request, offer_id):
    trade_offer = get_object_or_404(
        TradeOffer.objects.select_related('sender', 'receiver', 'requested_listing__card', 'requested_listing__user').prefetch_related('items__offered_user_card__card'),
        id=offer_id,
        status__in=['accepted', 'completed'],
    )
    meeting, _ = MeetingAgreement.objects.get_or_create(trade_offer=trade_offer)
    return _meeting_response(request, meeting, _meeting_context_for_trade_offer(trade_offer))


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
        'parsed_rows': [],
        'parsed_count': 0,
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
                user_card = UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=is_owned,
                    quantity_owned=quantity if is_owned else 0,
                    quantity_required=0 if is_owned else quantity,
                    desired_match_mode='exact_printing' if is_owned else row.get('desired_match_mode', 'exact_printing'),
                    listing_intent=row.get('listing_intent', 'sell'),
                    condition=row.get('condition', 'near_mint'),
                    asking_price=_to_decimal(row.get('asking_price')),
                )
                _create_match_alerts_for_user_card(user_card)
                created_count += 1

            messages.success(request, f'Se publicaron {created_count} cartas desde el lote.')
            return redirect('card_list')

        form = UploadFileForm(request.POST, request.FILES)
        context['form'] = form
        if form.is_valid():
            uploaded_file = request.FILES['file']
            parsed_rows = _parse_moxfield_csv(uploaded_file)
            context.update({
                'parsed_rows': parsed_rows,
                'parsed_count': len(parsed_rows),
            })
            if parsed_rows:
                messages.success(request, 'Archivo importado. La validacion con Scryfall se procesara por lotes.')
            else:
                messages.error(request, 'No se encontraron filas validas en el CSV.')

    return render(request, 'users/upload_file.html', context)

@login_required
def import_cards(request):
    if request.method == 'POST':
        try:
            extracted_data = json.loads(request.POST.get('extracted_data', '[]'))
            for data in extracted_data:
                user_card = UserCard.objects.create(
                    user=request.user,
                    card=Card.objects.get_or_create(name=data['nombre_carta'], set_code='')[0],
                    quantity_owned=int(data['cantidad']),
                    is_owned=True
                )
                _create_match_alerts_for_user_card(user_card)
            messages.success(request, 'Cartas importadas exitosamente a tus cartas en posesión.')
        except Exception as e:
            messages.error(request, f'Error al importar las cartas: {str(e)}')
        return redirect('card_list')

@login_required
@require_POST
def add_to_owned_cards(request):
    try:
        data = json.loads(request.POST.get('extracted_data', '[]'))
        for card in data:
            user_card = UserCard.objects.create(
                user=request.user,
                card=Card.objects.get_or_create(name=card['nombre_carta'], set_code='')[0],
                quantity_owned=int(card['cantidad']),
                is_owned=True
            )
            _create_match_alerts_for_user_card(user_card)
        return JsonResponse({'status': 'success', 'message': 'Cards added to owned list successfully.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

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
                user_card = UserCard.objects.create(
                    user=request.user,
                    card=card,
                    is_owned=False,
                    quantity_owned=0,
                    quantity_required=int(card_data['cantidad'])
                )
                _create_match_alerts_for_user_card(user_card)
            return JsonResponse({'status': 'success', 'message': 'User cards created successfully.'})
        except json.JSONDecodeError as e:
            return JsonResponse({'status': 'error', 'message': f'JSON decode error: {str(e)}'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
    return JsonResponse({'status': 'error', 'message': 'Invalid request method.'})


@login_required
def card_list(request):
    search_query = request.GET.get('q', '').strip()
    set_filter = request.GET.get('set', '').strip()
    rarity_filter = request.GET.get('rarity', '').strip()
    sort = request.GET.get('sort', 'newest').strip()

    base_queryset = UserCard.objects.filter(user=request.user).select_related('card')
    if search_query:
        base_queryset = base_queryset.filter(card__name__icontains=search_query)
    if set_filter:
        base_queryset = base_queryset.filter(
            Q(card__set_code__iexact=set_filter) | Q(card__set_name__iexact=set_filter)
        )
    if rarity_filter:
        base_queryset = base_queryset.filter(card__rarity__iexact=rarity_filter)

    sort_map = {
        'newest': '-id',
        'oldest': 'id',
        'price_desc': '-card__price',
        'price_asc': 'card__price',
    }
    ordering = sort_map.get(sort, '-id')

    owned_cards = base_queryset.filter(is_owned=True, quantity_owned__gt=0).order_by(ordering, '-id')[:9]
    desired_cards = base_queryset.filter(is_owned=False).order_by(ordering, '-id')[:9]

    owned_all = UserCard.objects.filter(user=request.user, is_owned=True, quantity_owned__gt=0).select_related('card')
    desired_all = UserCard.objects.filter(user=request.user, is_owned=False).select_related('card')

    total_collection_value = sum(card.total_price() for card in owned_all)
    total_owned_cards = sum(card.quantity_owned for card in owned_all)
    total_wanted_cards = sum(card.quantity_required for card in desired_all)

    set_options = [
        value for value in UserCard.objects.filter(user=request.user)
        .exclude(card__set_name__isnull=True)
        .exclude(card__set_name__exact='')
        .values_list('card__set_name', flat=True)
        .distinct()
        .order_by('card__set_name')
    ]
    rarity_options = [
        value for value in UserCard.objects.filter(user=request.user)
        .exclude(card__rarity__isnull=True)
        .exclude(card__rarity__exact='')
        .values_list('card__rarity', flat=True)
        .distinct()
        .order_by('card__rarity')
    ]

    return render(request, 'users/card_list.html', {
        'owned_cards': owned_cards,
        'desired_cards': desired_cards,
        'total_collection_value': total_collection_value,
        'total_owned_cards': total_owned_cards,
        'total_wanted_cards': total_wanted_cards,
        'set_options': set_options,
        'rarity_options': rarity_options,
        'active_filters': {
            'q': search_query,
            'set': set_filter,
            'rarity': rarity_filter,
            'sort': sort,
        },
    })


@login_required
def inventory_list(request):
    if request.method == 'POST':
        selected_ids = request.POST.getlist('selected_cards')
        selected_cards = UserCard.objects.filter(
            user=request.user,
            is_owned=True,
            id__in=selected_ids,
        )
        selected_count = selected_cards.count()
        bulk_action = request.POST.get('bulk_action', '').strip()
        if not selected_count:
            messages.error(request, 'Selecciona al menos una carta para aplicar la accion masiva.')
            return redirect('inventory_list')
        if bulk_action == 'delete':
            selected_cards.delete()
            messages.success(request, f'{selected_count} carta(s) eliminada(s) del inventario.')
        elif bulk_action == 'set_intent':
            new_intent = request.POST.get('bulk_intent', '').strip()
            if new_intent not in dict(UserCard.LISTING_INTENT_CHOICES):
                messages.error(request, 'Selecciona una intencion valida.')
                return redirect('inventory_list')
            selected_cards.update(listing_intent=new_intent)
            messages.success(request, f'{selected_count} carta(s) actualizada(s).')
        else:
            messages.error(request, 'Selecciona una accion masiva valida.')
        return redirect('inventory_list')

    search_query = request.GET.get('q', '').strip()
    set_filter = request.GET.get('set', '').strip()
    condition_filter = request.GET.get('condition', '').strip()
    intent_filter = request.GET.get('intent', '').strip()
    sort = request.GET.get('sort', 'newest').strip()

    inventory = UserCard.objects.filter(
        user=request.user,
        is_owned=True,
        quantity_owned__gt=0,
    ).select_related('card')

    if search_query:
        inventory = inventory.filter(card__name__icontains=search_query)
    if set_filter:
        inventory = inventory.filter(
            Q(card__set_code__iexact=set_filter) | Q(card__set_name__iexact=set_filter)
        )
    if condition_filter:
        inventory = inventory.filter(condition=condition_filter)
    if intent_filter:
        inventory = inventory.filter(listing_intent=intent_filter)

    sort_map = {
        'newest': '-id',
        'oldest': 'id',
        'name': 'card__name',
        'price_desc': '-asking_price',
        'price_asc': 'asking_price',
        'quantity_desc': '-quantity_owned',
    }
    inventory = inventory.order_by(sort_map.get(sort, '-id'), 'card__name')

    value_expression = ExpressionWrapper(
        Coalesce('asking_price', 'card__price') * F('quantity_owned'),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    inventory_totals = inventory.aggregate(
        total_units=Sum('quantity_owned'),
        total_value=Sum(value_expression),
    )
    total_cards = inventory.count()
    total_units = inventory_totals['total_units'] or 0
    total_value = inventory_totals['total_value'] or Decimal('0.00')

    inventory_page = Paginator(inventory, 25).get_page(request.GET.get('page'))

    set_options = [
        value for value in UserCard.objects.filter(user=request.user, is_owned=True, quantity_owned__gt=0)
        .exclude(card__set_name__isnull=True)
        .exclude(card__set_name__exact='')
        .values_list('card__set_name', flat=True)
        .distinct()
        .order_by('card__set_name')
    ]

    return render(request, 'users/inventory_list.html', {
        'inventory_cards': inventory_page,
        'inventory_page': inventory_page,
        'page_prefix': _pagination_prefix(request),
        'set_options': set_options,
        'condition_options': UserCard.CONDITION_CHOICES,
        'intent_options': UserCard.LISTING_INTENT_CHOICES,
        'total_cards': total_cards,
        'total_units': total_units,
        'total_value': total_value,
        'active_filters': {
            'q': search_query,
            'set': set_filter,
            'condition': condition_filter,
            'intent': intent_filter,
            'sort': sort,
        },
    })


@login_required
def wanted_inventory_list(request):
    if request.method == 'POST':
        selected_ids = request.POST.getlist('selected_cards')
        selected_cards = UserCard.objects.filter(
            user=request.user,
            is_owned=False,
            id__in=selected_ids,
        )
        selected_count = selected_cards.count()
        bulk_action = request.POST.get('bulk_action', '').strip()
        if not selected_count:
            messages.error(request, 'Selecciona al menos una carta buscada para aplicar la accion masiva.')
            return redirect('wanted_inventory_list')
        if bulk_action == 'delete':
            selected_cards.delete()
            messages.success(request, f'{selected_count} busqueda(s) eliminada(s).')
        elif bulk_action == 'set_match_mode':
            new_match_mode = request.POST.get('bulk_match_mode', '').strip()
            if new_match_mode not in dict(UserCard.DESIRED_MATCH_CHOICES):
                messages.error(request, 'Selecciona un modo de match valido.')
                return redirect('wanted_inventory_list')
            selected_cards.update(desired_match_mode=new_match_mode)
            messages.success(request, f'{selected_count} busqueda(s) actualizada(s).')
        else:
            messages.error(request, 'Selecciona una accion masiva valida.')
        return redirect('wanted_inventory_list')

    search_query = request.GET.get('q', '').strip()
    set_filter = request.GET.get('set', '').strip()
    match_mode_filter = request.GET.get('match_mode', '').strip()
    sort = request.GET.get('sort', 'newest').strip()

    wanted_cards = UserCard.objects.filter(
        user=request.user,
        is_owned=False,
    ).select_related('card')

    if search_query:
        wanted_cards = wanted_cards.filter(card__name__icontains=search_query)
    if set_filter:
        wanted_cards = wanted_cards.filter(
            Q(card__set_code__iexact=set_filter) | Q(card__set_name__iexact=set_filter)
        )
    if match_mode_filter:
        wanted_cards = wanted_cards.filter(desired_match_mode=match_mode_filter)

    sort_map = {
        'newest': '-id',
        'oldest': 'id',
        'name': 'card__name',
        'quantity_desc': '-quantity_required',
    }
    wanted_cards = wanted_cards.order_by(sort_map.get(sort, '-id'), 'card__name')

    wanted_totals = wanted_cards.aggregate(total_units=Sum('quantity_required'))
    total_cards = wanted_cards.count()
    total_units = wanted_totals['total_units'] or 0
    wanted_page = Paginator(wanted_cards, 25).get_page(request.GET.get('page'))

    set_options = [
        value for value in UserCard.objects.filter(user=request.user, is_owned=False)
        .exclude(card__set_name__isnull=True)
        .exclude(card__set_name__exact='')
        .values_list('card__set_name', flat=True)
        .distinct()
        .order_by('card__set_name')
    ]

    return render(request, 'users/wanted_inventory_list.html', {
        'wanted_cards': wanted_page,
        'wanted_page': wanted_page,
        'page_prefix': _pagination_prefix(request),
        'set_options': set_options,
        'match_mode_options': UserCard.DESIRED_MATCH_CHOICES,
        'total_cards': total_cards,
        'total_units': total_units,
        'active_filters': {
            'q': search_query,
            'set': set_filter,
            'match_mode': match_mode_filter,
            'sort': sort,
        },
    })


def marketplace(request):
    search_query = request.GET.get('q', '').strip()
    set_filter = request.GET.get('set', '').strip()
    rarity_filter = request.GET.get('rarity', '').strip()
    condition_filter = request.GET.get('condition', '').strip()
    min_price_filter = _to_decimal(request.GET.get('min_price'))
    max_price_filter = _to_decimal(request.GET.get('max_price'))
    sort = request.GET.get('sort', 'price_asc').strip()

    base_queryset = UserCard.objects.filter(
        is_owned=True,
        quantity_owned__gt=0,
        listing_intent__in=['sell', 'sell_trade'],
    ).select_related('card', 'user')
    if request.user.is_authenticated:
        base_queryset = base_queryset.exclude(user=request.user)
    if search_query:
        base_queryset = base_queryset.filter(
            Q(card__name__icontains=search_query) |
            Q(card__description__icontains=search_query)
        )
    if set_filter:
        base_queryset = base_queryset.filter(
            Q(card__set_code__iexact=set_filter) | Q(card__set_name__iexact=set_filter)
        )
    if rarity_filter:
        base_queryset = base_queryset.filter(card__rarity__iexact=rarity_filter)
    if condition_filter:
        base_queryset = base_queryset.filter(condition=condition_filter)
    if min_price_filter is not None:
        base_queryset = base_queryset.filter(asking_price__gte=min_price_filter)
    if max_price_filter is not None:
        base_queryset = base_queryset.filter(asking_price__lte=max_price_filter)

    sort_map = {
        'price_asc': 'asking_price',
        'price_desc': '-asking_price',
        'newest': '-id',
        'oldest': 'id',
        'name': 'card__name',
    }
    ordering = sort_map.get(sort, 'asking_price')

    grouped_queryset = base_queryset.values(
        'card_id',
        'card__name',
        'card__set_name',
        'card__set_code',
        'card__collector_number',
        'card__image_url',
        'card__rarity',
        'card__price',
    ).annotate(
        sellers_count=Count('id'),
        min_price=Min('asking_price'),
        max_price=Max('asking_price'),
    )

    grouped_sort_map = {
        'price_asc': ('min_price', 'card__name'),
        'price_desc': ('-min_price', 'card__name'),
        'newest': ('-card_id',),
        'oldest': ('card_id',),
        'name': ('card__name',),
    }
    grouped_ordering = grouped_sort_map.get(sort, ('min_price', 'card__name'))

    featured_cards = grouped_queryset.order_by('-sellers_count', '-max_price', 'card__name')[:3]
    listings_page = Paginator(
        grouped_queryset.order_by(*grouped_ordering),
        12,
    ).get_page(request.GET.get('page'))

    set_options = [
        value for value in UserCard.objects.filter(
            is_owned=True,
            quantity_owned__gt=0,
            listing_intent__in=['sell', 'sell_trade'],
        )
        .exclude(card__set_name__isnull=True)
        .exclude(card__set_name__exact='')
        .values_list('card__set_name', flat=True)
        .distinct()
        .order_by('card__set_name')
    ]
    rarity_options = [
        value for value in UserCard.objects.filter(
            is_owned=True,
            quantity_owned__gt=0,
            listing_intent__in=['sell', 'sell_trade'],
        )
        .exclude(card__rarity__isnull=True)
        .exclude(card__rarity__exact='')
        .values_list('card__rarity', flat=True)
        .distinct()
        .order_by('card__rarity')
    ]

    active_filter_labels = []
    if rarity_filter:
        active_filter_labels.append(f"Rarity: {rarity_filter}")
    if condition_filter:
        active_filter_labels.append(f"Condition: {condition_filter.replace('_', ' ').title()}")
    if min_price_filter is not None:
        active_filter_labels.append(f"Min price: ${_format_money(min_price_filter)}")
    if max_price_filter is not None:
        active_filter_labels.append(f"Max price: ${_format_money(max_price_filter)}")
    if set_filter:
        active_filter_labels.append(f"Set: {set_filter}")
    if search_query:
        active_filter_labels.append(f"Search: {search_query}")

    return render(request, 'users/marketplace.html', {
        'featured_cards': featured_cards,
        'listings': listings_page,
        'listings_page': listings_page,
        'page_prefix': _pagination_prefix(request),
        'set_options': set_options,
        'rarity_options': rarity_options,
        'condition_options': UserCard.CONDITION_CHOICES,
        'active_filters': {
            'q': search_query,
            'set': set_filter,
            'rarity': rarity_filter,
            'condition': condition_filter,
            'min_price': request.GET.get('min_price', '').strip(),
            'max_price': request.GET.get('max_price', '').strip(),
            'sort': sort,
        },
        'active_filter_labels': active_filter_labels,
    })


def marketplace_card_detail(request, card_id):
    card = get_object_or_404(Card, id=card_id)
    listings = UserCard.objects.filter(
        card=card,
        is_owned=True,
        quantity_owned__gt=0,
        listing_intent__in=['sell', 'sell_trade'],
    ).select_related('user', 'card')
    if request.user.is_authenticated:
        listings = listings.exclude(user=request.user)
    listings = listings.order_by('asking_price', 'id')

    summary = listings.aggregate(
        sellers_count=Count('id'),
        min_price=Min('asking_price'),
        max_price=Max('asking_price'),
    )

    return render(request, 'users/marketplace_card_detail.html', {
        'card': card,
        'listings': listings,
        'summary': summary,
    })


@login_required
def sale_listing_detail(request, listing_id):
    listing = get_object_or_404(
        UserCard.objects.select_related('user', 'card'),
        id=listing_id,
        is_owned=True,
        quantity_owned__gt=0,
    )
    if listing.user == request.user:
        messages.error(request, 'No puedes abrir la pantalla de compra para tu propio listing.')
        return redirect('marketplace_card_detail', card_id=listing.card_id)
    if listing.listing_intent not in ['sell', 'sell_trade']:
        messages.error(request, 'Este listing no esta disponible para compra directa.')
        return redirect('marketplace_card_detail', card_id=listing.card_id)

    market_price = _listing_value(listing)
    min_offer_amount = (market_price * Decimal('0.75')).quantize(Decimal('0.01'))
    max_offer_amount = (market_price * Decimal('3.00')).quantize(Decimal('0.01'))
    related_offers = UserCard.objects.filter(
        card=listing.card,
        is_owned=True,
        quantity_owned__gt=0,
        listing_intent__in=['sell', 'sell_trade'],
    ).exclude(id=listing.id).exclude(user=request.user).select_related('user', 'card').order_by('asking_price', 'id')[:4]

    related_cards = Card.objects.filter(
        set_name=listing.card.set_name,
    ).exclude(id=listing.card_id).exclude(image_url__isnull=True).exclude(image_url__exact='').order_by('-price', 'name')[:5]

    sale_history = Exchange.objects.filter(
        exchange_type='sale',
        status='completed',
        target_card=listing.card,
    )
    month_counts = {}
    for exchange in sale_history:
        key = (exchange.date.year, exchange.date.month)
        month_counts[key] = month_counts.get(key, 0) + 1

    history_buckets = []
    max_sales = 0
    for bucket_year, bucket_month in _month_buckets(6):
        count = month_counts.get((bucket_year, bucket_month), 0)
        max_sales = max(max_sales, count)
        history_buckets.append({
            'label': date(bucket_year, bucket_month, 1).strftime('%b'),
            'count': count,
        })

    divisor = max_sales or 1
    for bucket in history_buckets:
        bucket['height'] = max(16, int((bucket['count'] / divisor) * 140)) if bucket['count'] else 16

    total_sales = sale_history.count()
    available_units = listing.quantity_owned

    return render(request, 'users/sale_listing_detail.html', {
        'listing': listing,
        'market_price': market_price,
        'min_offer_amount': min_offer_amount,
        'max_offer_amount': max_offer_amount,
        'market_price_input': f"{market_price.quantize(Decimal('0.01'))}",
        'min_offer_amount_input': f"{min_offer_amount}",
        'max_offer_amount_input': f"{max_offer_amount}",
        'related_offers': related_offers,
        'related_cards': related_cards,
        'history_buckets': history_buckets,
        'total_sales': total_sales,
        'available_units': available_units,
    })


def _trade_candidates_for_listing(current_user, target_listing):
    desired_cards = list(UserCard.objects.filter(
        user=target_listing.user,
        is_owned=False,
    ).select_related('card'))
    exact_card_ids = {
        desired.card_id
        for desired in desired_cards
        if desired.desired_match_mode == 'exact_printing'
    }
    any_printing_names = {
        desired.card.normalized_name
        for desired in desired_cards
        if desired.desired_match_mode == 'any_printing' and desired.card.normalized_name
    }

    owned_by_sender = UserCard.objects.filter(
        user=current_user,
        is_owned=True,
        quantity_owned__gt=0,
    )
    candidate_filter = Q()
    if exact_card_ids:
        candidate_filter |= Q(card_id__in=exact_card_ids)
    if any_printing_names:
        candidate_filter |= Q(card__normalized_name__in=any_printing_names)
    if not candidate_filter:
        return []
    owned_by_sender = owned_by_sender.filter(candidate_filter).select_related('card')

    exact_owned_by_card_id = {}
    any_owned_by_name = {}
    for owned_card in owned_by_sender:
        exact_owned_by_card_id.setdefault(owned_card.card_id, []).append(owned_card)
        if owned_card.card.normalized_name:
            any_owned_by_name.setdefault(owned_card.card.normalized_name, []).append(owned_card)

    candidates = []
    for desired_card in desired_cards:
        if desired_card.desired_match_mode == 'any_printing':
            matching_owned_cards = any_owned_by_name.get(desired_card.card.normalized_name, [])
        else:
            matching_owned_cards = exact_owned_by_card_id.get(desired_card.card_id, [])
        for owned_match in matching_owned_cards:
            max_quantity = min(
                max(owned_match.quantity_owned, 1),
                max(desired_card.quantity_required or 1, 1),
            )
            candidates.append({
                'desired_user_card': desired_card,
                'offered_user_card': owned_match,
                'value': _listing_value(owned_match),
                'max_quantity': max_quantity,
            })
    return candidates


@login_required
def initiate_trade(request, listing_id):
    target_listing = get_object_or_404(
        UserCard.objects.select_related('user', 'card'),
        id=listing_id,
        is_owned=True,
        quantity_owned__gt=0,
    )
    if target_listing.user == request.user:
        messages.error(request, 'No puedes negociar tu propia carta desde esta pantalla.')
        return redirect('marketplace_card_detail', card_id=target_listing.card_id)

    requested_value = _listing_value(target_listing)
    candidate_rows = _trade_candidates_for_listing(request.user, target_listing)
    candidate_map = {str(row['offered_user_card'].id): row for row in candidate_rows}
    selected_ids = request.POST.getlist('selected_offer_cards') if request.method == 'POST' else []
    selected_rows = [candidate_map[row_id] for row_id in selected_ids if row_id in candidate_map]
    offered_value = sum((row['value'] for row in selected_rows), Decimal('0.00'))
    cash_due = requested_value - offered_value
    if cash_due < Decimal('0.00'):
        cash_due = Decimal('0.00')
    market_spread = requested_value - offered_value

    if request.method == 'POST' and request.POST.get('action') == 'seal':
        trade_offer = TradeOffer.objects.create(
            sender=request.user,
            receiver=target_listing.user,
            requested_listing=target_listing,
            requested_listing_price=requested_value,
            offered_cards_value=offered_value,
            cash_due=cash_due,
            message=request.POST.get('message', '').strip(),
        )
        for row in selected_rows:
            TradeOfferItem.objects.create(
                trade_offer=trade_offer,
                offered_user_card=row['offered_user_card'],
                quantity=1,
                valuation_price=row['value'],
            )

        selected_names = ', '.join(row['offered_user_card'].card.name for row in selected_rows) or 'sin cartas adjuntas'
        Notification.objects.create(
            sender=request.user,
            receiver=target_listing.user,
            title=f"Oferta de intercambio: {target_listing.card.name}",
            message=(
                f"{request.user.username} propuso intercambio por '{target_listing.card.name}'. "
                f"Cartas ofrecidas: {selected_names}. Saldo restante: ${_format_money(cash_due)}."
            ),
            category='trade',
            action_url=_trade_offer_action_url(trade_offer),
            type='action',
        )
        messages.success(request, 'La propuesta de intercambio fue enviada correctamente.')
        return redirect('list_exchanges')

    matched_count = len(candidate_rows)
    unavailable_count = UserCard.objects.filter(user=target_listing.user, is_owned=False).count() - matched_count

    return render(request, 'users/trade_hub.html', {
        'target_listing': target_listing,
        'candidate_rows': candidate_rows,
        'selected_ids': selected_ids,
        'selected_rows': selected_rows,
        'requested_value': requested_value,
        'offered_value': offered_value,
        'cash_due': cash_due,
        'market_spread': market_spread,
        'matched_count': matched_count,
        'unavailable_count': max(unavailable_count, 0),
    })


@login_required
def trade_offer_review(request, offer_id):
    _expire_stale_offers()
    trade_offer = TradeOffer.objects.select_related('sender', 'receiver', 'requested_listing__card').prefetch_related(
        'items__offered_user_card__card'
    ).filter(
        id=offer_id,
        receiver=request.user,
        status='pending',
    ).first()
    if not trade_offer:
        messages.error(request, 'Esta oferta de intercambio ya no esta disponible. Puede haber sido respondida o caducada.')
        return redirect('active_ritual_offers')
    return render(request, 'users/trade_offer_review.html', {
        'trade_offer': trade_offer,
    })


@login_required
@require_POST
def accept_trade_offer(request, offer_id):
    _expire_stale_offers()
    trade_offer = TradeOffer.objects.filter(id=offer_id, receiver=request.user, status='pending').first()
    if not trade_offer:
        messages.error(request, 'Esta oferta de intercambio ya no esta disponible. Puede haber sido respondida o caducada.')
        return redirect('active_ritual_offers')
    trade_offer.status = 'accepted'
    trade_offer.save()
    MeetingAgreement.objects.get_or_create(trade_offer=trade_offer)
    Notification.objects.create(
        sender=request.user,
        receiver=trade_offer.sender,
        title=f"Intercambio aceptado: {trade_offer.requested_listing.card.name}",
        message=f"{request.user.username} accepted your trade offer for {trade_offer.requested_listing.card.name}.",
        category='trade',
        action_url=reverse('meeting_trade_offer', args=[trade_offer.id]),
        type='info'
    )
    messages.success(request, 'Oferta de intercambio aceptada.')
    return redirect('meeting_trade_offer', offer_id=trade_offer.id)


@login_required
@require_POST
def reject_trade_offer(request, offer_id):
    _expire_stale_offers()
    trade_offer = TradeOffer.objects.filter(id=offer_id, receiver=request.user, status='pending').first()
    if not trade_offer:
        messages.error(request, 'Esta oferta de intercambio ya no esta disponible. Puede haber sido respondida o caducada.')
        return redirect('active_ritual_offers')
    trade_offer.status = 'rejected'
    trade_offer.save()
    Notification.objects.create(
        sender=request.user,
        receiver=trade_offer.sender,
        message=f"{request.user.username} rejected your trade offer for {trade_offer.requested_listing.card.name}.",
        type='info'
    )
    messages.success(request, 'Oferta de intercambio rechazada.')
    return redirect('active_ritual_offers')
