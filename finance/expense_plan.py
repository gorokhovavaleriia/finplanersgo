"""Планирование расходов — год -> месяц -> неделя -> день (см. PLAN.md,
"новая логика планирования расходов"). Зеркалит finance.income_plan
структурно (месяц — источник истины, вводится в годовом виде, каскадом
делится по неделям день-в-день) и, как и она, синхронизируется в ОБЕ
стороны:

- Правка снизу (день -> неделя, неделя -> месяц) ВСЕГДА поднимается наверх
  как сумма того, что мельче — без тумблера "текущий/отредактированный".
  День поднимается в неделю при сохранении дневного вида (apply_daily_plan
  пишет день, сумму по дням в неделю поднимает вызывающий код в views.py,
  см. plan_save/PLANDAY); неделя поднимается в месяц через
  sync_monthly_from_weeks (см. её докстринг) — вызывается после правки
  недели в месячном виде.
- Правка сверху (месяц -> недели, неделя -> дни), наоборот, спускается ВНИЗ
  (пересчитывает то, что мельче, поровну) — но только когда значение
  реально изменилось, и только если внизу уже есть данные, на клиенте
  сначала спрашивают подтверждение (см. plan_grid.html); сервер просто
  выполняет то, что подтвердили — см.
  redistribute_week_to_days/apply_monthly_plan."""

import calendar
from datetime import date, timedelta

from django.db.models import Sum

from .logic import aggregate
from .models import ExpenseDailyPlan, ExpenseMonthlyPlan, PlanEntry


def _daily_rate(monthly_amount, year, month):
    days = calendar.monthrange(year, month)[1]
    return monthly_amount / days if days else 0.0


def _week_amount_from_months(group, category, subcategory, direction, week_start, week_end):
    """Сумма плана на неделю = сумма вкладов всех месяцев, чьи дни попадают
    в эту неделю, по их собственным (текущим) месячным планам — 0, если
    план на этот месяц ещё не вводили (см. income_plan._week_income_from_months,
    тот же принцип)."""
    total = 0.0
    months_touched = {(week_start.year, week_start.month), (week_end.year, week_end.month)}
    for year, month in months_touched:
        try:
            monthly = ExpenseMonthlyPlan.objects.get(
                group=group, category=category, subcategory=subcategory, direction=direction, year=year, month=month,
            )
        except ExpenseMonthlyPlan.DoesNotExist:
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


def apply_monthly_plan(group, category, subcategory, direction, year, month, amount):
    """Сохраняет месячный план и пересчитывает недели этого месяца (и,
    если задевает граница, соседних месяцев тоже) — а также дни внутри
    каждой такой недели (redistribute_week_to_days). Без этого правка
    месяца "сверху" (например, обнуление) останавливалась на неделях: если
    у недели уже была своя дневная разбивка (её кто-то вводил отдельно),
    старые дни оставались как были, и именно в них смотрит дневной вид и
    проекция остатка (planned_daily_fund_expense — предпочитает явные дни
    недельной сумме, если они есть) — снаружи казалось бы, что обнуление
    ничего не стёрло. Подтверждение перезаписи уже спросили на клиенте
    (см. plan_grid.html, data-has-lower у месячных ячеек)."""
    ExpenseMonthlyPlan.objects.update_or_create(
        group=group, category=category, subcategory=subcategory, direction=direction, year=year, month=month,
        defaults={"amount": amount},
    )
    for week_start, week_end in aggregate.month_weeks(year, month):
        total = _week_amount_from_months(group, category, subcategory, direction, week_start, week_end)
        PlanEntry.objects.update_or_create(
            group=group, category=category, subcategory=subcategory, direction=direction, week_start=week_start,
            defaults={"amount": total},
        )
        redistribute_week_to_days(group, category, subcategory, direction, week_start, total)


def get_monthly_plan(group, category, subcategory, direction, year, month):
    try:
        return float(ExpenseMonthlyPlan.objects.get(
            group=group, category=category, subcategory=subcategory, direction=direction, year=year, month=month,
        ).amount)
    except ExpenseMonthlyPlan.DoesNotExist:
        return 0.0


def get_weekly_plan(group, category, subcategory, direction, week_start):
    try:
        return float(PlanEntry.objects.get(
            group=group, category=category, subcategory=subcategory, direction=direction, week_start=week_start,
        ).amount)
    except PlanEntry.DoesNotExist:
        return 0.0


def apply_weekly_plan(group, category, subcategory, direction, week_start, amount):
    PlanEntry.objects.update_or_create(
        group=group, category=category, subcategory=subcategory, direction=direction, week_start=week_start,
        defaults={"amount": amount},
    )


def get_daily_plan(group, category, subcategory, direction, day):
    """None, если день не редактировали отдельно — тогда для показа надо
    брать дефолт (недельный план / 7)."""
    try:
        return float(ExpenseDailyPlan.objects.get(
            group=group, category=category, subcategory=subcategory, direction=direction, day=day,
        ).amount)
    except ExpenseDailyPlan.DoesNotExist:
        return None


def apply_daily_plan(group, category, subcategory, direction, day, amount):
    ExpenseDailyPlan.objects.update_or_create(
        group=group, category=category, subcategory=subcategory, direction=direction, day=day,
        defaults={"amount": amount},
    )


def sync_monthly_from_weeks(group, category, subcategory, direction, year, month):
    """Пересчитывает месячную сумму (ExpenseMonthlyPlan) как сумму ВСЕХ
    недель этого месяца (aggregate.month_weeks) — вызывается после правки
    отдельной недели в месячном виде, чтобы месяц всегда оставался суммой
    того, что видно по неделям, а не отдельной, разъезжающейся с ними
    цифрой (то же самое, что и income_plan.sync_monthly_from_weeks — по
    просьбе пользователя расходы должны вести себя так же). В отличие от
    apply_monthly_plan (та, наоборот, разносит месяц ПО неделям), сами
    недели не трогает — только читает и складывает."""
    total = sum(
        get_weekly_plan(group, category, subcategory, direction, week_start)
        for week_start, _week_end in aggregate.month_weeks(year, month)
    )
    ExpenseMonthlyPlan.objects.update_or_create(
        group=group, category=category, subcategory=subcategory, direction=direction, year=year, month=month,
        defaults={"amount": total},
    )


def redistribute_week_to_days(group, category, subcategory, direction, week_start, amount):
    """Делит недельную сумму поровну на 7 дней и переписывает дневной план
    (ExpenseDailyPlan) — вызывается, когда неделю правят "сверху" (в
    месячном виде) и пользователь подтвердил перезапись дней."""
    daily = amount / 7.0
    for i in range(7):
        day = week_start + timedelta(days=i)
        apply_daily_plan(group, category, subcategory, direction, day, daily)


def week_has_daily_data(group, category, subcategory, direction, week_start):
    """Есть ли у этой недели уже свои дневные данные — если да, правка
    недели "сверху" должна сначала спросить подтверждение, иначе тихо
    перезапишет то, что пользователь мог вручную поправить по дням."""
    end = week_start + timedelta(days=6)
    return ExpenseDailyPlan.objects.filter(
        group=group, category=category, subcategory=subcategory, direction=direction, day__gte=week_start, day__lte=end,
    ).exists()


def month_has_weekly_data(group, category, subcategory, direction, year, month):
    """Есть ли у недель этого месяца уже свой (ненулевой) план — если да,
    правка месяца "сверху" (в годовом виде) должна сначала спросить
    подтверждение."""
    week_starts = [w[0] for w in aggregate.month_weeks(year, month)]
    return PlanEntry.objects.filter(
        group=group, category=category, subcategory=subcategory, direction=direction, week_start__in=week_starts,
    ).exclude(amount=0).exists()


def recompute_category_monthly(group, category, subcategory, year, month):
    """Пересчитывает месячный план категории целиком как сумму её
    направлений (из базы, а не только из текущего запроса) — используется
    и при обычном сохранении, и при мгновенном сохранении одной ячейки
    (см. views.plan_save_field). Возвращает новую сумму — вызывающий код
    использует её, чтобы решить, блокировать ли ячейку категории целиком
    (сумма ненулевая) или вернуть её в редактируемое состояние (сумма 0,
    направления пустые)."""
    total = ExpenseMonthlyPlan.objects.filter(
        group=group, category=category, subcategory=subcategory, year=year, month=month,
    ).exclude(direction="").aggregate(total=Sum("amount"))["total"] or 0
    if total:
        apply_monthly_plan(group, category, subcategory, "", year, month, float(total))
    return float(total)


def recompute_category_weekly(group, category, subcategory, week_start):
    """Пересчитывает недельный план категории целиком как сумму её
    направлений — тот же принцип, что и recompute_category_monthly."""
    total = PlanEntry.objects.filter(
        group=group, category=category, subcategory=subcategory, week_start=week_start,
    ).exclude(direction="").aggregate(total=Sum("amount"))["total"] or 0
    if total:
        apply_weekly_plan(group, category, subcategory, "", week_start, float(total))
    return float(total)
