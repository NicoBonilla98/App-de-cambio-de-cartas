from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.shortcuts import render
from django.db.models import Count, Max, Q, Sum
from users.models import Card, CustomUser, Exchange, TradeOffer, TradeOfferItem, UserCard


def _popular_activity_cards(limit=3):
    activity_by_card = {}

    sale_rows = Exchange.objects.filter(
        status='completed',
        exchange_type='sale',
        target_card__isnull=False,
    ).values('target_card').annotate(
        sold_count=Count('id'),
        total_value=Sum('agreed_price'),
        last_activity=Max('date'),
    )
    for row in sale_rows:
        stats = activity_by_card.setdefault(row['target_card'], {
            'sold_count': 0,
            'traded_count': 0,
            'total_value': 0,
            'last_activity': None,
        })
        stats['sold_count'] += row['sold_count'] or 0
        stats['total_value'] += row['total_value'] or 0
        stats['last_activity'] = max(
            filter(None, [stats['last_activity'], row['last_activity']]),
            default=None,
        )

    requested_trade_rows = TradeOffer.objects.filter(
        status='completed',
        requested_listing__card__isnull=False,
    ).values('requested_listing__card').annotate(
        traded_count=Count('id'),
        last_activity=Max('updated_at'),
    )
    for row in requested_trade_rows:
        card_id = row['requested_listing__card']
        stats = activity_by_card.setdefault(card_id, {
            'sold_count': 0,
            'traded_count': 0,
            'total_value': 0,
            'last_activity': None,
        })
        stats['traded_count'] += row['traded_count'] or 0
        stats['last_activity'] = max(
            filter(None, [stats['last_activity'], row['last_activity']]),
            default=None,
        )

    offered_trade_rows = TradeOfferItem.objects.filter(
        trade_offer__status='completed',
        offered_user_card__card__isnull=False,
    ).values('offered_user_card__card').annotate(
        traded_count=Sum('quantity'),
        last_activity=Max('trade_offer__updated_at'),
    )
    for row in offered_trade_rows:
        card_id = row['offered_user_card__card']
        stats = activity_by_card.setdefault(card_id, {
            'sold_count': 0,
            'traded_count': 0,
            'total_value': 0,
            'last_activity': None,
        })
        stats['traded_count'] += row['traded_count'] or 0
        stats['last_activity'] = max(
            filter(None, [stats['last_activity'], row['last_activity']]),
            default=None,
        )

    cards = Card.objects.in_bulk(activity_by_card.keys())
    popular_cards = []
    for card_id, stats in activity_by_card.items():
        card = cards.get(card_id)
        if not card:
            continue
        activity_count = stats['sold_count'] + stats['traded_count']
        popular_cards.append({
            'card': card,
            'sold_count': stats['sold_count'],
            'traded_count': stats['traded_count'],
            'activity_count': activity_count,
            'total_value': stats['total_value'],
            'last_activity': stats['last_activity'],
            'score': (stats['sold_count'] * 3) + (stats['traded_count'] * 2),
            'is_fallback': False,
        })

    popular_cards.sort(
        key=lambda item: (item['score'], item['activity_count'], item['last_activity'] or ''),
        reverse=True,
    )
    if popular_cards:
        return popular_cards[:limit]

    fallback_rows = UserCard.objects.filter(
        is_owned=True,
        quantity_owned__gt=0,
        listing_intent__in=['sell', 'sell_trade'],
    ).values('card').annotate(
        sellers_count=Count('id'),
        min_price=Sum('asking_price'),
        last_activity=Max('id'),
    ).order_by('-sellers_count', '-last_activity')[:limit]
    fallback_cards = Card.objects.in_bulk([row['card'] for row in fallback_rows])
    return [
        {
            'card': fallback_cards[row['card']],
            'sold_count': 0,
            'traded_count': 0,
            'activity_count': row['sellers_count'],
            'total_value': 0,
            'last_activity': None,
            'score': row['sellers_count'],
            'is_fallback': True,
        }
        for row in fallback_rows
        if row['card'] in fallback_cards
    ]


def home(request):
    completed_sales = Exchange.objects.filter(status='completed', exchange_type='sale').count()
    completed_trades = TradeOffer.objects.filter(status='completed').count()
    context = {
        'featured_cards': Card.objects.order_by('-price', 'name')[:3],
        'popular_cards': _popular_activity_cards(),
        'active_collectors': CustomUser.objects.count(),
        'listed_cards': Card.objects.count(),
        'active_exchanges': completed_sales + completed_trades,
    }
    return render(request, 'home.html', context)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'),  # Redirige a la página principal
    path('users/', include('users.urls')),  # Incluye las URLs de la app users
    path('', home, name='home'),  # Ruta para la pantalla de inicio
]
