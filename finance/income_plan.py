"""Разбивка плана поступлений год -> месяц -> неделя -> день (см. PLAN.md,
"планирование доходов"). В отличие от плана расходов, здесь два источника
истины: IncomeMonthlyPlan (месячная сумма, вводится в годовом виде) и
IncomePlanEntry (недельная сумма — либо пересчитана из месячной, либо
исправлена вручную прямо в месячном/недельном виде).

Недельная сумма всегда пересчитывается заново из месячных планов ОБОИХ
месяцев, которые попадают в эту неделю (см. _week_income_from_months) —
поэтому граничная неделя (заходящая в соседний месяц) остаётся верной,
даже если сначала сохранили один месяц, а потом другой."""

import calendar
from datetime import date, timedelta

from .logic import aggregate
from .models import IncomeDailyPlan, IncomeMonthlyPlan, IncomePlanEntry, IncomeWeekendFlag

# У направления и у "категории/группы целиком" раньше был один и тот же
# ключ хранения — direction=None (целиком) превращался в "" точно так же,
# как направление "" (это реальное направление "(без направления)",
# встречается у операций без заполненного направления). Из-за этого поле
# формы для категории целиком и поле для направления "(без направления)"
# получали ОДНО И ТО ЖЕ имя (и один и тот же ключ в базе) — правка одного
# перетирала другое молча. EMPTY_DIRECTION_KEY — отдельный, гарантированно
# не пустой маркер именно для направления "(без направления)"; direction=None
# (целиком) как и раньше хранится под "". Живёт здесь (а не в views.py),
# чтобы finance.plan_projection мог использовать тот же ключ без цикличного
# импорта views.py.
EMPTY_DIRECTION_KEY = "␀без-направления"


def dir_key(direction):
    if direction is None:
        return ""
    if direction == "":
        return EMPTY_DIRECTION_KEY
    return direction


def load_weekend_flags():
    """{(категория, ключ_направления) -> works_weekends (bool)} — ключ
    направления такой же, каким его передаёт вызывающий код в
    set_weekend_flag (см. views._dir_key: "" — категория целиком, иначе
    направление, в т.ч. отдельный маркер для реального направления
    "(без направления)"). Без записи в базе считаем обычным (по будням)."""
    return {(f.category, f.direction): f.works_weekends for f in IncomeWeekendFlag.objects.all()}


def set_weekend_flag(category, direction_key, works_weekends):
    IncomeWeekendFlag.objects.update_or_create(
        category=category, direction=direction_key, defaults={"works_weekends": works_weekends},
    )


def _daily_rate(monthly_amount, year, month):
    days = calendar.monthrange(year, month)[1]
    return monthly_amount / days if days else 0.0


def _week_income_from_months(category, direction, week_start, week_end):
    """Сумма плана на неделю = сумма вкладов всех месяцев, чьи дни попадают
    в эту неделю, по их собственным (текущим) месячным планам — 0, если
    план на этот месяц ещё не вводили."""
    total = 0.0
    months_touched = {(week_start.year, week_start.month), (week_end.year, week_end.month)}
    for year, month in months_touched:
        try:
            monthly = IncomeMonthlyPlan.objects.get(category=category, direction=direction, year=year, month=month)
        except IncomeMonthlyPlan.DoesNotExist:
            continue
        rate = _daily_rate(float(monthly.amount), year, month)
        m_start = date(year, month, 1)
        m_end = date(year, month, calendar.monthrange(year, month)[1])
        overlap_start = max(week_start, m_start)
        overlap_end = min(week_end, m_end)
        if overlap_start > overlap_end:
            continue
        days_in_week = (overlap_end - overlap_start).days + 1
        total += rate * days_in_week
    return total


def apply_monthly_plan(category, direction, year, month, amount):
    """Сохраняет месячный план и пересчитывает недели этого месяца (и,
    если задевает граница, соседних месяцев тоже — их недели, попавшие в
    этот месяц, могли измениться)."""
    direction = direction or ""
    IncomeMonthlyPlan.objects.update_or_create(
        category=category, direction=direction, year=year, month=month,
        defaults={"amount": amount},
    )
    for week_start, week_end in aggregate.month_weeks(year, month):
        total = _week_income_from_months(category, direction, week_start, week_end)
        IncomePlanEntry.objects.update_or_create(
            category=category, direction=direction, week_start=week_start,
            defaults={"amount": total},
        )


def apply_weekly_plan(category, direction, week_start, amount):
    """Прямая правка недельного плана (в месячном/недельном виде) — просто
    перезаписывает неделю; если потом пересохранят месячный план, эта
    правка будет пересчитана заново (см. модуль docstring)."""
    IncomePlanEntry.objects.update_or_create(
        category=category, direction=direction or "", week_start=week_start,
        defaults={"amount": amount},
    )


def redistribute_week_to_days(category, direction, week_start, amount, works_weekends):
    """Делит недельную сумму по дням (через daily_split — 5 будних или 7
    дней, в зависимости от works_weekends) и переписывает дневной план
    (IncomeDailyPlan) — вызывается, когда неделю правят "сверху" (в
    месячном виде). Без этого правка недели меняет только IncomePlanEntry,
    а старые (уже неактуальные) записи по дням остаются как были — из-за
    этого planned_daily_income (день за днём смотрит именно в дневные
    данные, если они есть) считает по устаревшим дням, а не по новой
    недельной сумме, и разъезжается с таблицей плана поступлений, которая
    просто суммирует IncomePlanEntry напрямую."""
    for day, value in daily_split(amount, week_start, works_weekends):
        if value is not None:
            apply_daily_plan(category, direction, day, value)


def sync_monthly_from_weeks(category, direction, year, month):
    """Пересчитывает месячную сумму (IncomeMonthlyPlan) как сумму ВСЕХ
    недель этого месяца (aggregate.month_weeks) — вызывается после правки
    любой отдельной недели в месячном виде, чтобы месяц всегда оставался
    суммой того, что видно по неделям, а не отдельной, разъезжающейся с
    ними цифрой (тот же принцип, что и апдейт недели из суммы дней в
    redistribute_week_to_days/apply_daily_plan-цепочке, только уровнем
    выше). В отличие от apply_monthly_plan (та, наоборот, разносит месяц ПО
    неделям), сами недели не трогает — только читает и складывает."""
    total = sum(
        get_weekly_plan(category, direction, week_start)
        for week_start, _week_end in aggregate.month_weeks(year, month)
    )
    IncomeMonthlyPlan.objects.update_or_create(
        category=category, direction=direction or "", year=year, month=month,
        defaults={"amount": total},
    )


def week_has_daily_data(category, direction, week_start):
    """Есть ли у этой недели уже свои дневные данные — если да, правка
    недели "сверху" должна сначала спросить подтверждение (см.
    redistribute_week_to_days), иначе тихо перезапишет то, что пользователь
    мог вручную поправить по дням."""
    end = week_start + timedelta(days=6)
    return IncomeDailyPlan.objects.filter(
        category=category, direction=direction or "", day__gte=week_start, day__lte=end,
    ).exists()


def get_daily_plan(category, direction, day):
    """None, если день не редактировали отдельно — тогда для показа надо
    брать дефолт из daily_split(недельный_план)."""
    try:
        return float(IncomeDailyPlan.objects.get(category=category, direction=direction or "", day=day).amount)
    except IncomeDailyPlan.DoesNotExist:
        return None


def apply_daily_plan(category, direction, day, amount):
    IncomeDailyPlan.objects.update_or_create(
        category=category, direction=direction or "", day=day, defaults={"amount": amount},
    )


def load_daily_plan_map():
    """{(категория, направление, день) -> сумма} — вся таблица одним
    запросом (см. load_weekly_plan_map — тот же приём). Используется, чтобы
    решить, есть ли у недели УЖЕ хоть один явно заданный день — если да,
    остальные дни этой недели считаются нулём, а не дефолтом от
    daily_split(), см. views._day_cells и plan_projection.planned_daily_income."""
    return {(e.category, e.direction, e.day): float(e.amount) for e in IncomeDailyPlan.objects.all()}


def load_weekly_plan_map():
    """{(категория, направление, week_start) -> сумма} — вся таблица одним
    запросом. Без этого get_weekly_plan в цикле по колонкам/строкам
    (planned_daily_income, _income_plan_table_month/_week) дёргал отдельный
    SELECT на каждую ячейку — на годовой вид это тысячи запросов, основная
    причина медленной загрузки и зависания при сохранении страницы "План"."""
    return {(e.category, e.direction, e.week_start): float(e.amount) for e in IncomePlanEntry.objects.all()}


def load_monthly_plan_map():
    """То же самое, но для IncomeMonthlyPlan (годовой вид) — см. load_weekly_plan_map."""
    return {(e.category, e.direction, e.year, e.month): float(e.amount) for e in IncomeMonthlyPlan.objects.all()}


def get_weekly_plan(category, direction, week_start, plan_map=None):
    direction = direction or ""
    if plan_map is not None:
        return plan_map.get((category, direction, week_start), 0.0)
    try:
        return float(IncomePlanEntry.objects.get(category=category, direction=direction, week_start=week_start).amount)
    except IncomePlanEntry.DoesNotExist:
        return 0.0


def get_monthly_plan(category, direction, year, month, plan_map=None):
    direction = direction or ""
    if plan_map is not None:
        return plan_map.get((category, direction, year, month), 0.0)
    try:
        return float(IncomeMonthlyPlan.objects.get(category=category, direction=direction, year=year, month=month).amount)
    except IncomeMonthlyPlan.DoesNotExist:
        return 0.0


def daily_split(weekly_amount, week_start, works_weekends):
    """[(день, план_или_None), ...] на 7 дней недели, начиная с week_start
    (понедельник) — план только на рабочие дни (5 будних, либо все 7, если
    категория работает без выходных); в выходные — None (не пишем)."""
    work_days = 7 if works_weekends else 5
    daily_amount = weekly_amount / work_days if work_days else 0.0
    result = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        is_weekend = day.weekday() >= 5  # 5=суббота, 6=воскресенье
        if is_weekend and not works_weekends:
            result.append((day, None))
        else:
            result.append((day, daily_amount))
    return result
