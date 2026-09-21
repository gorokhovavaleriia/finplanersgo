from datetime import date

from django.core.management.base import BaseCommand, CommandError

from finance.fintablo import FintabloError
from finance.fintablo_sync import DEFAULT_SYNC_FROM, incremental_sync_from, sync_operations


class Command(BaseCommand):
    help = (
        "Синхронизирует операции ДДС из API Финтабло в Operation. "
        "Без аргументов — только недавние дни (от последней уже загруженной "
        "операции с запасом в пару дней, см. incremental_sync_from): старые "
        "даты в Финтабло задним числом почти не меняются, перечитывать их "
        "заново на каждый прогон незачем. --full-since-2026 — вся история "
        f"с {DEFAULT_SYNC_FROM.isoformat()} (приложение всё равно не считает "
        "фонды раньше 30.08.2026 — глубже не нужно). --full-history — вообще "
        "вся история Финтабло, без нижней границы (медленнее всего)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date-from", help="Нижняя граница периода, ГГГГ-ММ-ДД", default=None)
        parser.add_argument("--date-to", help="Верхняя граница периода, ГГГГ-ММ-ДД", default=None)
        parser.add_argument(
            "--full-since-2026", action="store_true",
            help=f"Синхронизировать всю историю с {DEFAULT_SYNC_FROM.isoformat()}, а не только недавние дни",
        )
        parser.add_argument(
            "--full-history", action="store_true",
            help="Синхронизировать вообще всю историю Финтабло, без нижней границы (медленно)",
        )

    def handle(self, *args, **options):
        date_from = _parse_iso(options["date_from"])
        if date_from is None and not options["full_history"]:
            date_from = DEFAULT_SYNC_FROM if options["full_since_2026"] else incremental_sync_from()
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
