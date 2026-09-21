"""Плановый остаток по фондам — страница "План" (см. PLAN.md, строка 136:
"добавляются сюда плановые поступления и плановые остатки"). Устроено как
finance.fund_balance (день за днём, с DISTRIBUTION_START), но вместо
фактических операций берёт плановые цифры: плановые поступления
(finance.income_plan, по категориям/направлениям) и плановый расход фонда
(PlanEntry). В отличие от фактического остатка, НЕ ограничивается последней
датой с операциями — это прогноз, поэтому считается вперёд на весь
запрошенный период, даже если он весь в будущем."""

from collections import defaultdict
from datetime import timedelta

from . import income_plan
from .fund_balance import DISTRIBUTION_START, resolve_share
from .logic import aggregate
from .models import PlanEntry

_dir_key = income_plan.dir_key


def _week_start(day):
    return day - timedelta(days=day.weekday())


def _income_leaf_specs(classified, income_categories):
    """[(категория, направление_или_None), ...] — листья дерева
    поступлений, ровно как их видит форма плана (см. views._income_plan_rows):
    у категорий с несколькими направлениями план вводится по направлениям,
    у остальных — по категории целиком."""
    specs = []
    for category in income_categories:
        directions = aggregate.income_directions(classified, category)
        if len(directions) > 1:
            specs.extend((category, d) for d in directions)
        else:
            specs.append((category, None))
    return specs


def planned_daily_income(classified, income_categories, weekend_flags, start_date, end_date, weekly_plan_map=None):
    """{день: плановое поступление за день} — сумма по всем листьям дерева
    поступлений, каждый недельный план поделён на дни через тот же
    income_plan.daily_split, что и в дневном виде (для категорий, которые
    туда не делятся, флаг "без выходных" всегда False — 5 будних дней).

    weekly_plan_map — заранее загруженный income_plan.load_weekly_plan_map()
    (передаётся вызывающей стороной, чтобы не грузить одну и ту же таблицу
    заново на каждый вызов — эта функция зовётся дважды за один рендер
    страницы "План"); если не передан, грузится сам одним запросом."""
    if end_date < start_date:
        return {}
    if weekly_plan_map is None:
        weekly_plan_map = income_plan.load_weekly_plan_map()
    specs = _income_leaf_specs(classified, income_categories)
    result = defaultdict(float)
    week_cache = {}
    day = start_date
    while day <= end_date:
        ws = _week_start(day)
        if ws not in week_cache:
            per_day = defaultdict(float)
            for category, direction in specs:
                dir_key = _dir_key(direction)
                weekly = income_plan.get_weekly_plan(category, dir_key, ws, plan_map=weekly_plan_map)
                works_weekends = weekend_flags.get((category, dir_key), False)
                for d, v in income_plan.daily_split(weekly, ws, works_weekends):
                    per_day[d] += v or 0.0
            week_cache[ws] = per_day
        result[day] = week_cache[ws].get(day, 0.0)
        day += timedelta(days=1)
    return result


def planned_daily_fund_expense(fund_names, start_date, end_date):
    """{фонд: {день: плановый расход фонда за день}} — недельный PlanEntry
    (группа = фонд) поделён поровну на 7 дней (у расходов дня недели, в
    отличие от поступлений, не бывает)."""
    by_fund_week = defaultdict(lambda: defaultdict(float))
    for p in PlanEntry.objects.filter(group__in=fund_names):
        by_fund_week[p.group][p.week_start] += float(p.amount)

    result = {fund: {} for fund in fund_names}
    if end_date < start_date:
        return result
    for fund in fund_names:
        by_week = by_fund_week.get(fund, {})
        day = start_date
        while day <= end_date:
            ws = _week_start(day)
            result[fund][day] = by_week.get(ws, 0.0) / 7.0
            day += timedelta(days=1)
    return result


def planned_daily_balances(classified, income_categories, weekend_flags, all_funds, up_to_date, transfer_deltas=None, weekly_plan_map=None, shares_map=None):
    """{фонд: {день: плановый остаток на конец дня}} с DISTRIBUTION_START
    по up_to_date включительно — та же механика, что и
    fund_balance.actual_daily_balances, но на плановых цифрах вместо
    фактических операций. transfer_deltas — ручные перемещения между
    фондами (см. fund_balance.load_transfer_deltas) — это уже случившийся
    факт, а не прогноз, поэтому учитывается и в плановом остатке тоже: раз
    перевод точно был/будет, прогноз должен его видеть. shares_map — см.
    fund_balance.load_income_shares/resolve_share, те же переопределения
    доли по месяцам, что и в фактическом остатке (иначе план и факт
    разъедутся по разным % на одной странице "План")."""
    transfer_deltas = transfer_deltas or {}
    shares_map = shares_map or {}
    fund_names = list(all_funds.keys())
    result = {fund: {} for fund in fund_names}
    if up_to_date < DISTRIBUTION_START:
        return result

    daily_income = planned_daily_income(classified, income_categories, weekend_flags, DISTRIBUTION_START, up_to_date, weekly_plan_map=weekly_plan_map)
    daily_fund_expense = planned_daily_fund_expense(fund_names, DISTRIBUTION_START, up_to_date)

    running = {fund: info["balance"] for fund, info in all_funds.items()}
    day = DISTRIBUTION_START
    while day <= up_to_date:
        day_income_total = daily_income.get(day, 0.0)
        for fund, info in all_funds.items():
            share = resolve_share(fund, day.year, day.month, all_funds, shares_map)
            fund_income = day_income_total * share
            fund_expense = daily_fund_expense.get(fund, {}).get(day, 0.0)
            fund_transfer = transfer_deltas.get((fund, day), 0.0)
            running[fund] = running[fund] - fund_expense + fund_income + fund_transfer
            result[fund][day] = running[fund]
        day += timedelta(days=1)
    return result


def balance_at(daily_balances, fund, day):
    return daily_balances.get(fund, {}).get(day)
