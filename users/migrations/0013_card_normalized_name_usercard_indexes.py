from django.db import migrations, models


def populate_normalized_names(apps, schema_editor):
    Card = apps.get_model('users', 'Card')
    cards = []
    for card in Card.objects.all().only('id', 'name', 'normalized_name'):
        normalized_name = ' '.join((card.name or '').strip().lower().split())
        if card.normalized_name != normalized_name:
            card.normalized_name = normalized_name
            cards.append(card)
    if cards:
        Card.objects.bulk_update(cards, ['normalized_name'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_meetingagreement'),
    ]

    operations = [
        migrations.AddField(
            model_name='card',
            name='normalized_name',
            field=models.CharField(blank=True, db_index=True, max_length=180),
        ),
        migrations.RunPython(populate_normalized_names, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='usercard',
            index=models.Index(fields=['user', 'is_owned'], name='users_userc_user_id_403432_idx'),
        ),
        migrations.AddIndex(
            model_name='usercard',
            index=models.Index(fields=['is_owned', 'card'], name='users_userc_is_owne_202875_idx'),
        ),
        migrations.AddIndex(
            model_name='usercard',
            index=models.Index(fields=['is_owned', 'desired_match_mode'], name='users_userc_is_owne_024d3f_idx'),
        ),
        migrations.AddIndex(
            model_name='usercard',
            index=models.Index(fields=['listing_intent', 'is_owned'], name='users_userc_listing_eb98ca_idx'),
        ),
    ]
