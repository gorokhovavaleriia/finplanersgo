"""Разбивка операций по годам/месяцам/неделям для сетки на экране — плюс
границы недель (понедельник-воскресенье, с заходом в соседние месяцы, как в
макете) и сборка итоговых таблиц по категориям/подкатегориям."""

import calendar
from datetime import date, timedelta

from . import classify
from . import expenses

MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def classify_operations(operations, income_statya_map, overrides=None,
                         expense_fund_map=None, expense_overrides=None):
    """Помечает каждую операцию category/subcategory (поступления, знак +,
    subcategory всегда None — подкатегорий у поступлений больше нет) и
    exp_group/exp_category/exp_subcategory (расходы, знак -, subcategory
    всегда None) — по classify.classify_income / expenses.classify_expense,
    либо по ручной правке (overrides / expense_overrides: {id(op) в списке
    -> результат}), которая побеждает автоматическую. Поле для расходов
    заполняется только если передан expense_fund_map (иначе везде None).

    Отдельно помечает "excluded" (переводы между счетами, взаиморасчёты) —
    независимо от знака суммы: это не поступление и не расход, такие строки
    не должны попадать даже в список операций на экране (см. classify.is_excluded)."""
    overrides = overrides or {}
    expense_overrides = expense_overrides or {}
    result = []
    for i, op in enumerate(operations):
        op = dict(op)
        op["excluded"] = classify.is_excluded(op["statya"])

        if i in overrides:
            op["category"], op["subcategory"] = overrides[i]
        elif op["amount"] > 0:
            classified = classify.classify_income(op, income_statya_map)
            if classified is None:
                op["category"], op["subcategory"] = None, None
            else:
                op["category"], op["subcategory"] = classified
        else:
            op["category"], op["subcategory"] = None, None

        if expense_fund_map is None:
            op["exp_group"], op["exp_category"], op["exp_subcategory"] = None, None, None
        elif i in expense_overrides:
            op["exp_group"], op["exp_category"], op["exp_subcategory"] = expense_overrides[i]
        elif op["amount"] < 0 and not op["excluded"]:
            op["exp_group"], op["exp_category"], op["exp_subcategory"] = expenses.classify_expense(
                op, expense_fund_map
            )
        else:
            op["exp_group"], op["exp_category"], op["exp_subcategory"] = None, None, None

        result.append(op)
    return result


def years_present(operations):
    years = sorted({op["date"].year for op in operations})
    return years or [date.today().year]


def month_weeks(year, month):
    """[(start, end), ...] — недели пн-вс, покрывающие весь месяц (первая и
    последняя неделя могут заходить в соседние месяцы, см. макет)."""
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    start = first - timedelta(days=first.weekday())
    weeks = []
    while start <= last:
        end = start + timedelta(days=6)
        weeks.append((start, end))
        start = end + timedelta(days=1)
    return weeks


def category_rows(income_categories):
    """[(category, subcategory_or_None), ...] в порядке из operation_map —
    строки итоговой таблицы, строго по тому, что реально есть в листе
    "Поступления" (там уже есть своя категория "Неразнесенное поступление" —
    отдельную придуманную "Нераспределенное" сверху больше не добавляем)."""
    rows = []
    for category, subs in income_categories.items():
        if subs:
            for sub in subs:
                rows.append((category, sub))
        else:
            rows.append((category, None))
    return rows


def _matches_row(op, category, subcategory):
    return op["category"] == category and op["subcategory"] == subcategory


def period_total(classified_ops, category, subcategory, start, end):
    return sum(
        op["amount"] for op in classified_ops
        if _matches_row(op, category, subcategory) and start <= op["date"] <= end
    )


def year_table(classified_ops, income_categories, year):
    """rows: [(category, subcategory)], columns: [(label, month_int)] за все
    12 месяцев + значения, плюс месячные и построчные итоги."""
    rows = category_rows(income_categories)
    columns = [(MONTH_NAMES[m - 1], m) for m in range(1, 13)]
    data = {}
    for category, subcategory in rows:
        for _, month in columns:
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            data[(category, subcategory, month)] = period_total(
                classified_ops, category, subcategory, start, end
            )
    return rows, columns, data


def month_table(classified_ops, income_categories, year, month):
    rows = category_rows(income_categories)
    weeks = month_weeks(year, month)
    columns = [(f"{s.strftime('%d.%m.%Y')}-{e.strftime('%d.%m.%Y')}", (s, e)) for s, e in weeks]
    data = {}
    for category, subcategory in rows:
        for _, key in columns:
            start, end = key
            data[(category, subcategory, key)] = period_total(
                classified_ops, category, subcategory, start, end
            )
    return rows, columns, data


def week_table(classified_ops, income_categories, start, end):
    rows = category_rows(income_categories)
    days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    columns = [(d.strftime("%d.%m"), d) for d in days]
    data = {}
    for category, subcategory in rows:
        for _, d in columns:
            data[(category, subcategory, d)] = period_total(classified_ops, category, subcategory, d, d)
    return rows, columns, data


# Строки вида "План" перебирают каждую категорию/направление/колонку
# периода отдельным вызовом (для годового вида — это тысячи вызовов), а
# каждый такой вызов раньше линейно сканировал ВЕСЬ classified_ops (там
# уже больше 10 тысяч операций после перехода на синхронизацию с Финтабло)
# — например 3204 вызова expense_direction_period_total x ~13000 операций
# каждый = больше 50 млн сравнений на один рендер годового вида, отсюда и
# медленная загрузка (см. обсуждение с пользователем — искали причину,
# оказалось не Финтабло, а этот O(операции x ячейки) перебор).
#
# Вместо этого индексируем classified_ops ОДИН раз на весь список вызовов
# (он один и тот же объект на все ячейки одного рендера — _plan_context/
# _expense_table строят его один раз в начале и переиспользуют), и потом
# каждый вызов работает только с уже отфильтрованным маленьким списком
# записей нужной категории/направления, а не со всеми операциями. Кэш —
# по identity списка (один на последний обработанный classified_ops):
# для однопоточного dev-сервера этого достаточно, но не потокобезопасно.

_income_index_cache = {"ref": None, "index": None}
_expense_index_cache = {"ref": None, "index": None}


def _income_index(classified_ops):
    """{категория: {направление: [(дата, сумма), ...]}} — только операции
    с проставленной категорией поступления."""
    if _income_index_cache["ref"] is not classified_ops:
        index = {}
        for op in classified_ops:
            if op["category"] is None:
                continue
            by_direction = index.setdefault(op["category"], {})
            by_direction.setdefault(op["direction"].strip(), []).append((op["date"], op["amount"]))
        _income_index_cache["ref"] = classified_ops
        _income_index_cache["index"] = index
    return _income_index_cache["index"]


def _expense_index(classified_ops):
    """{(группа, категория, подкатегория): {направление: [(дата, |сумма|), ...]}}
    — только операции с проставленной категорией расхода."""
    if _expense_index_cache["ref"] is not classified_ops:
        index = {}
        for op in classified_ops:
            if op["exp_group"] is None:
                continue
            key = (op["exp_group"], op["exp_category"], op["exp_subcategory"])
            by_direction = index.setdefault(key, {})
            by_direction.setdefault(op["direction"].strip(), []).append((op["date"], abs(op["amount"])))
        _expense_index_cache["ref"] = classified_ops
        _expense_index_cache["index"] = index
    return _expense_index_cache["index"]


def total_income_for_period(classified_ops, start, end):
    """Общая сумма поступлений (по всем категориям) за период — для
    справочной строки "Поступления" в шапке каждой группы на виде "План"."""
    total = 0.0
    for by_direction in _income_index(classified_ops).values():
        for entries in by_direction.values():
            total += sum(amount for d, amount in entries if start <= d <= end)
    return total


def income_directions(classified_ops, category):
    """Отсортированный список направлений, встречающихся у операций этой
    категории поступления — если их больше одного, у строки в сетке
    показываем стрелочку разворота (подкатегорий у поступлений больше нет,
    разворот идёт прямо по направлению; пустое направление — последним)."""
    dirs = _income_index(classified_ops).get(category, {}).keys()
    return sorted(dirs, key=lambda d: (d == "", d))


def income_direction_period_total(classified_ops, category, direction, start, end):
    entries = _income_index(classified_ops).get(category, {}).get(direction, [])
    return sum(amount for d, amount in entries if start <= d <= end)


def expense_directions(classified_ops, group, category, subcategory):
    """Отсортированный список направлений ("Направление" из исходной
    таблицы), встречающихся у операций этой категории/подкатегории расходов
    — если их больше одного, у строки в сетке показываем стрелочку разворота
    по направлениям (пустое направление — последним, отдельной строкой)."""
    dirs = _expense_index(classified_ops).get((group, category, subcategory), {}).keys()
    return sorted(dirs, key=lambda d: (d == "", d))


def expense_direction_period_total(classified_ops, group, category, subcategory, direction, start, end):
    entries = _expense_index(classified_ops).get((group, category, subcategory), {}).get(direction, [])
    return sum(amount for d, amount in entries if start <= d <= end)


def expense_period_total(classified_ops, group, category, subcategory, start, end):
    """Сумма по модулю (расходы хранятся со знаком минус — на экране их
    показываем как положительные суммы, как принято в фин. отчётах)."""
    by_direction = _expense_index(classified_ops).get((group, category, subcategory), {})
    total = 0.0
    for entries in by_direction.values():
        total += sum(amount for d, amount in entries if start <= d <= end)
    return total


def expense_year_table(classified_ops, rows, year):
    columns = [(MONTH_NAMES[m - 1], m) for m in range(1, 13)]
    data = {}
    for group, category, subcategory in rows:
        for _, month in columns:
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            data[(group, category, subcategory, month)] = expense_period_total(
                classified_ops, group, category, subcategory, start, end
            )
    return rows, columns, data


def expense_month_table(classified_ops, rows, year, month):
    weeks = month_weeks(year, month)
    columns = [(f"{s.strftime('%d.%m.%Y')}-{e.strftime('%d.%m.%Y')}", (s, e)) for s, e in weeks]
    data = {}
    for group, category, subcategory in rows:
        for _, key in columns:
            start, end = key
            data[(group, category, subcategory, key)] = expense_period_total(
                classified_ops, group, category, subcategory, start, end
            )
    return rows, columns, data


def expense_week_table(classified_ops, rows, start, end):
    days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    columns = [(d.strftime("%d.%m"), d) for d in days]
    data = {}
    for group, category, subcategory in rows:
        for _, d in columns:
            data[(group, category, subcategory, d)] = expense_period_total(
                classified_ops, group, category, subcategory, d, d
            )
    return rows, columns, data
