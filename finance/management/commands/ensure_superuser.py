import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Создаёт суперпользователя из DJANGO_SUPERUSER_USERNAME/
    DJANGO_SUPERUSER_PASSWORD, если его ещё нет. Безопасно запускать на
    каждом старте контейнера (Amvera создаёт БД заново при первом деплое,
    а мы не переносили файл базы туда физически, см. AMVERA_DEPLOY.md) —
    после первого раза просто ничего не делает."""

    help = "Создаёт суперпользователя из переменных окружения, если его ещё нет."

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        if not username or not password:
            self.stdout.write("DJANGO_SUPERUSER_USERNAME/DJANGO_SUPERUSER_PASSWORD не заданы — пропускаю.")
            return

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Пользователь {username!r} уже есть — пропускаю.")
            return

        User.objects.create_superuser(username=username, email="", password=password)
        self.stdout.write(self.style.SUCCESS(f"Создан суперпользователь {username!r}."))
