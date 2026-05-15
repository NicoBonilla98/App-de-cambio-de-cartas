from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


def backfill_exchange_relations(apps, schema_editor):
    Card = apps.get_model('users', 'Card')
    Exchange = apps.get_model('users', 'Exchange')
    UserCard = apps.get_model('users', 'UserCard')

    updates = []
    sale_exchanges = Exchange.objects.filter(exchange_type='sale').only(
        'id',
        'sender_cards',
        'receiver_cards',
        'offer_amount',
        'listing_id',
        'target_card_id',
        'agreed_price',
    )
    for exchange in sale_exchanges:
        listing = None
        listing_ref = (exchange.sender_cards or '').strip()
        if listing_ref.startswith('listing:'):
            listing_id = listing_ref.split(':', 1)[1]
            if listing_id.isdigit():
                listing = UserCard.objects.filter(id=int(listing_id)).select_related('card').first()

        if listing is None and exchange.receiver_cards:
            listing = UserCard.objects.filter(
                user_id=exchange.receiver_id,
                is_owned=True,
                card__name__iexact=exchange.receiver_cards,
            ).select_related('card').order_by('id').first()

        if listing is not None:
            exchange.listing_id = listing.id
            exchange.target_card_id = listing.card_id
            fallback_price = listing.asking_price or listing.card.price or Decimal('0.00')
        else:
            target_card = Card.objects.filter(name__iexact=exchange.receiver_cards).order_by('id').first()
            exchange.target_card_id = target_card.id if target_card else None
            fallback_price = target_card.price if target_card else Decimal('0.00')

        exchange.agreed_price = exchange.offer_amount or fallback_price
        updates.append(exchange)

    if updates:
        Exchange.objects.bulk_update(updates, ['listing', 'target_card', 'agreed_price'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0013_card_normalized_name_usercard_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='exchange',
            name='agreed_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='exchange',
            name='listing',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='sale_exchanges',
                to='users.usercard',
            ),
        ),
        migrations.AddField(
            model_name='exchange',
            name='target_card',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='targeted_exchanges',
                to='users.card',
            ),
        ),
        migrations.RunPython(backfill_exchange_relations, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='exchange',
            index=models.Index(fields=['exchange_type', 'status'], name='users_excha_exchang_113997_idx'),
        ),
        migrations.AddIndex(
            model_name='exchange',
            index=models.Index(fields=['target_card', 'status'], name='users_excha_target__ec1dcf_idx'),
        ),
        migrations.AddIndex(
            model_name='exchange',
            index=models.Index(fields=['listing', 'status'], name='users_excha_listing_034e31_idx'),
        ),
    ]
