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

from .fintablo import list_categories, list_directions, list_transactions
from .models import Operation

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
    параллельный источник; см. обсуждение при первом внедрении синхронизации."""
    category_names = _category_name_map(list_categories())
    direction_names = _direction_name_map(list_directions())

    date_from_str = date_from.strftime("%d.%m.%Y") if date_from else None
    date_to_str = date_to.strftime("%d.%m.%Y") if date_to else None
    items = list_transactions(date_from_str, date_to_str)

    seen_external_ids = set()
    created = updated = 0
    for item in _expand_splits(items):
        if item.get("group") not in IMPORTED_GROUPS or item.get("isPlan"):
            continue

        external_id = str(item["id"])
        seen_external_ids.add(external_id)

        statya, rod_statya = category_names.get(item.get("categoryId"), ("", ""))
        direction = direction_names.get(item.get("directionId"), "")
        value = float(item.get("value") or 0)
        amount = -value if item["group"] == "outcome" else value

        _obj, was_created = Operation.objects.update_or_create(
            external_id=external_id,
            defaults={
                "date": datetime.strptime(item["date"], "%d.%m.%Y").date(),
                "amount": amount,
                "statya": statya,
                "rod_statya": rod_statya,
                "direction": direction,
                "description": item.get("description") or "",
                "source": Operation.SOURCE_FINTABLO_API,
            },
        )
        created += was_created
        updated += not was_created

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

    return created, updated, deleted_api, deleted_excel
