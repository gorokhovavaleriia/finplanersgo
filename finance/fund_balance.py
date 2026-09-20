"""Фактический (и плановый) остаток по фондам — см. PLAN.md, "распределение
доходов по фондам". С 30.08.2026 каждый день: остаток фонда = остаток на
начало дня - расходы фонда за день + доля фонда от поступлений за день.
До 30.08.2026 остаток не считается (см. DISTRIBUTION_START).

"Фонд чистой прибыли" не участвует в автоматическом распределении
поступлений (доли у него нет в листе "Фонды") — его остаток здесь считается
только по расходам, доля поступлений всегда 0, пока не появится отдельное
правило (см. PLAN.md: "будем делать вручную, распишу позже")."""

from datetime import date, timedelta

from .logic import aggregate, expenses
from .models import FundTransfer

DISTRIBUTION_START = date(2026, 8, 30)


def load_all_funds():
    """OrderedDict{фонд -> {"balance": ..., "share": ...}} — то же, что
    expenses.load_fund_balances(), плюс "Фонд чистой прибыли" с нулевым
    начальным остатком и нулевой долей (в листе "Фонды" его нет вообще)."""
    funds = expenses.load_fund_balances()
    funds.setdefault("Фонд чистой прибыли", {"balance": 0.0, "share": 0.0})
    return funds


def _fund_expense_on_day(classified_ops, fund, day):
    return sum(abs(op["amount"]) for op in classified_ops if op["exp_group"] == fund and op["date"] == day)


def load_transfer_deltas():
    """{(фонд, день): чистое изменение остатка от ручных перемещений между
    фондами за этот день} — у фонда-источника отрицательное, у фонда
    назначения положительное; несколько переводов в один день у одного
    фонда схлопываются в одну сумму (см. views.fund_transfers — форма
    "Перемещение денег")."""
    deltas = {}
    for t in FundTransfer.objects.all():
        amount = float(t.amount)
        deltas[(t.from_fund, t.date)] = deltas.get((t.from_fund, t.date), 0.0) - amount
        deltas[(t.to_fund, t.date)] = deltas.get((t.to_fund, t.date), 0.0) + amount
    return deltas


def actual_daily_balances(classified_ops, funds, up_to_date, transfer_deltas=None):
    """{фонд: {день: остаток_на_конец_дня}} — по всем дням от
    DISTRIBUTION_START до up_to_date включительно (пусто, если up_to_date
    раньше старта распределения). transfer_deltas — см. load_transfer_deltas,
    не глобальный state: чтобы одну и ту же выгрузку не делать заново на
    каждый фонд/период, вызывающий код загружает её один раз и передаёт сюда."""
    transfer_deltas = transfer_deltas or {}
    running = {fund: info["balance"] for fund, info in funds.items()}
    result = {fund: {} for fund in funds}
    if up_to_date < DISTRIBUTION_START:
        return result

    day = DISTRIBUTION_START
    while day <= up_to_date:
        day_income_total = aggregate.total_income_for_period(classified_ops, day, day)
        for fund, info in funds.items():
            fund_income = day_income_total * info["share"]
            fund_expense = _fund_expense_on_day(classified_ops, fund, day)
            fund_transfer = transfer_deltas.get((fund, day), 0.0)
            running[fund] = running[fund] - fund_expense + fund_income + fund_transfer
            result[fund][day] = running[fund]
        day += timedelta(days=1)
    return result


def balance_at(daily_balances, fund, day):
    """Остаток фонда на конец дня day, или None — если day раньше старта
    распределения, или после самого последнего дня, который посчитали."""
    return daily_balances.get(fund, {}).get(day)
