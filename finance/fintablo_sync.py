"""Синхронизация операций ДДС из Финтабло в Operation — сопоставляет
categoryId/directionId (числа в API) в текстовые статья/род.статья/
направление, как их ожидает finance.logic (те же поля, что заполняет
Excel-вариант, см. finance.logic.operations.read_operations), чтобы вся
остальная бизнес-логика (классификация, фонды, план) не знала, откуда
пришли операции.

Идемпотентно по Operation.external_id (id операции в Финтабло) — повторный
запуск обновляет изменившиеся операции и подчищает удалённые, не плодя
дублей."""

from datetime import date, datetime

from django.db import transaction

from .fintablo import list_categories, list_directions, list_transactions
from .models import Operation

# Для bulk_update — сколько строк отправлять в одном SQL-запросе. Sqlite
# по умолчанию ограничивает запрос ~999 параметрами, а тут на строку уходит
# 7 полей + id; 100 строк — с большим запасом.
UPDATE_BATCH_SIZE = 100

# "Полная" синхронизация (без явных дат) не тянет вообще всю историю
# Финтабло (там она уходит в 2021 год и на порядок замедляет каждую
# страницу — Operation.objects.all() грузится целиком при любом открытии) —
# приложение всё равно считает фонды только с 30.08.2026, глубже не нужно.
# Явный date_from в sync_operations() по-прежнему позволяет уйти дальше.
DEFAULT_SYNC_FROM = date(2026, 1, 1)

# group=transfer — перевод между своими счетами, не поступление и не
# расход; group=income/outcome — то, что нас интересует. Плановые операции
# Финтабло (isPlan) тоже пропускаем — план в этом приложении свой, отдельный
# (см. finance.expense_plan/income_plan), смешивать источники нельзя.
IMPORTED_GROUPS = {"income", "outcome"}


def _category_name_map(categories):
    """{id -> (статья, род.статья)} — род.статья — имя родительской статьи,
    если она есть, иначе пусто (тот же формат, что в строках
    operation_map.xlsx, см. finance.logic.expenses._read_mapping_rows)."""
    by_id = {c["id"]: c for c in categories}
    names = {}
    for cat in categories:
        parent = by_id.get(cat.get("parentId"))
        names[cat["id"]] = (cat.get("name") or "", (parent.get("name") or "") if parent else "")
    return names


def _direction_name_map(directions):
    return {d["id"]: (d.get("name") or "") for d in directions}


def _expand_splits(items):
    """Разбитая (split) операция приходит из /v1/transaction одной записью
    без своих categoryId/directionId — сумма и распределение по статьям
    лежат во вложенном "subs" (не задокументировано в OpenAPI-схеме,
    обнаружено по факту в ответе API). Дата/описание/группа/isPlan у частей
    свои не бывают — наследуются от родителя. Обычные (неразбитые) операции
    возвращаются как есть."""
    for item in items:
        subs = item.get("subs")
        if not subs:
            yield item
            continue
        for sub in subs:
            part = dict(item)
            part.update(sub)
            yield part


def sync_operations(date_from=None, date_to=None):
    """date_from/date_to — datetime.date или None (без ограничения с этой
    стороны). Возвращает (создано, обновлено, удалено_api, удалено_excel).

    В пределах запрошенного периода операции, которых Финтабло больше не
    вернул (удалили, перевели в план или в transfer на его стороне),
    удаляются и здесь — как "Удалить существующие" при загрузке Excel, но
    автоматически и только внутри синхронизируемого периода.

    Старые вручную загруженные из Excel операции (source=excel) в том же
    периоде тоже удаляются — иначе одна и та же реальная операция посчитается
    дважды (раз из Excel, раз из API). Переход на API — это замена Excel, не
    параллельный источник; см. обсуждение при первом внедрении синхронизации.

    Пишет пачками (bulk_create/bulk_update), а не по одной операции —
    на нескольких месяцах истории отдельный SELECT+INSERT/UPDATE+commit на
    каждую запись легко превышает таймаут веб-сервера (так и вышло на
    Amvera — gunicorn убивал воркер посреди синхронизации)."""
    category_names = _category_name_map(list_categories())
    direction_names = _direction_name_map(list_directions())

    date_from_str = date_from.strftime("%d.%m.%Y") if date_from else None
    date_to_str = date_to.strftime("%d.%m.%Y") if date_to else None
    items = list_transactions(date_from_str, date_to_str)

    rows = []
    seen_external_ids = set()
    for item in _expand_splits(items):
        if item.get("group") not in IMPORTED_GROUPS or item.get("isPlan"):
            continue

        external_id = str(item["id"])
        seen_external_ids.add(external_id)

        statya, rod_statya = category_names.get(item.get("categoryId"), ("", ""))
        direction = direction_names.get(item.get("directionId"), "")
        value = float(item.get("value") or 0)
        amount = -value if item["group"] == "outcome" else value

        rows.append({
            "external_id": external_id,
            "date": datetime.strptime(item["date"], "%d.%m.%Y").date(),
            "amount": amount,
            "statya": statya,
            "rod_statya": rod_statya,
            "direction": direction,
            "description": item.get("description") or "",
        })

    update_fields = ["date", "amount", "statya", "rod_statya", "direction", "description"]

    with transaction.atomic():
        existing = {
            op.external_id: op
            for op in Operation.objects.filter(external_id__in=seen_external_ids)
        }

        to_create = []
        to_update = []
        for row in rows:
            existing_op = existing.get(row["external_id"])
            if existing_op is None:
                to_create.append(Operation(source=Operation.SOURCE_FINTABLO_API, **row))
            else:
                for field in update_fields:
                    setattr(existing_op, field, row[field])
                existing_op.source = Operation.SOURCE_FINTABLO_API
                to_update.append(existing_op)

        Operation.objects.bulk_create(to_create)
        Operation.objects.bulk_update(to_update, update_fields + ["source"], batch_size=UPDATE_BATCH_SIZE)

        stale_qs = Operation.objects.filter(source=Operation.SOURCE_FINTABLO_API).exclude(
            external_id__in=seen_external_ids
        )
        if date_from:
            stale_qs = stale_qs.filter(date__gte=date_from)
        if date_to:
            stale_qs = stale_qs.filter(date__lte=date_to)
        deleted_api, _ = stale_qs.delete()

        excel_qs = Operation.objects.filter(source=Operation.SOURCE_EXCEL)
        if date_from:
            excel_qs = excel_qs.filter(date__gte=date_from)
        if date_to:
            excel_qs = excel_qs.filter(date__lte=date_to)
        deleted_excel, _ = excel_qs.delete()

    return len(to_create), len(to_update), deleted_api, deleted_excel
