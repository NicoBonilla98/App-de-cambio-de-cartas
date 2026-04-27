from django.core.management.base import BaseCommand
from users.models import CustomUser

class Command(BaseCommand):
    help = 'Populate missing data for all existing users.'

    def handle(self, *args, **kwargs):
        users_without_data = CustomUser.objects.filter(data_field__isnull=True)
        for user in users_without_data:
            user.data_field = 'default_value'
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Updated data for user: {user.username}'))

        self.stdout.write(self.style.SUCCESS('All missing data have been updated.'))