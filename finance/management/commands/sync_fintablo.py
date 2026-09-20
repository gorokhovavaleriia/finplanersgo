from datetime import date

from django.core.management.base import BaseCommand, CommandError

from finance.fintablo import FintabloError
from finance.fintablo_sync import DEFAULT_SYNC_FROM, sync_operations


class Command(BaseCommand):
    help = (
        "Синхронизирует операции ДДС из API Финтабло в Operation. "
        f"Без --date-from — с {DEFAULT_SYNC_FROM.isoformat()} по сегодня "
        "(приложение всё равно не считает фонды раньше 30.08.2026 — глубже "
        "не нужно, а вся история из Финтабло на порядок медленнее)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date-from", help="Нижняя граница периода, ГГГГ-ММ-ДД", default=None)
        parser.add_argument("--date-to", help="Верхняя граница периода, ГГГГ-ММ-ДД", default=None)
        parser.add_argument(
            "--full-history", action="store_true",
            help="Игнорировать умолчание и синхронизировать вообще всю историю (медленно)",
        )

    def handle(self, *args, **options):
        date_from = _parse_iso(options["date_from"])
        if date_from is None and not options["full_history"]:
            date_from = DEFAULT_SYNC_FROM
        date_to = _parse_iso(options["date_to"])
        try:
            created, updated, deleted_api, deleted_excel = sync_operations(date_from, date_to)
        except FintabloError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(
            f"Готово: создано {created}, обновлено {updated}, удалено (API) {deleted_api}, "
            f"удалено старых Excel-операций {deleted_excel}"
        ))


def _parse_iso(value):
    return date.fromisoformat(value) if value else None
