import calendar
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from . import expense_plan, fund_balance, income_plan, plan_export, plan_projection, services
from .fintablo import FintabloError
from .fintablo_sync import DEFAULT_SYNC_FROM, incremental_sync_from, sync_operations
from .forms import FundTransferForm, UploadOperationsForm
from .logic import aggregate, expenses, operations
from .models import (
    ExpenseDailyPlan, ExpenseMonthlyPlan, FundBalanceSnapshot, FundIncomeShare, FundTransfer, Operation, PlanEntry,
)


@permission_required("finance.add_operation", raise_exception=True)
def upload_operations(request):
    if request.method == "POST":
        form = UploadOperationsForm(request.POST, request.FILES)
        if form.is_valid():
            if form.cleaned_data["replace_existing"]:
                Operation.objects.filter(source=Operation.SOURCE_EXCEL).delete()
            rows = operations.read_operations(form.cleaned_data["file"])
            Operation.objects.bulk_create(
                Operation(
                    date=row["date"], amount=row["amount"], statya=row["statya"],
                    rod_statya=row["rod_statya"], direction=row["direction"],
                    description=row["description"], source=Operation.SOURCE_EXCEL,
                )
                for row in rows
            )
            messages.success(request, f"Загружено операций: {len(rows)}")
            return redirect("upload")
    else:
        form = UploadOperationsForm()
    total = Operation.objects.count()
    fintablo_total = Operation.objects.filter(source=Operation.SOURCE_FINTABLO_API).count()
    return render(request, "finance/upload.html", {"form": form, "total": total, "fintablo_total": fintablo_total})


@permission_required("finance.add_operation", raise_exception=True)
def sync_fintablo_now(request):
    """Синхронизация "по кнопке" — от последней уже загруженной операции (с
    запасом, см. incremental_sync_from), а не всей истории заново: старые
    даты в Финтабло задним числом почти никогда не меняются, а тянуть и
    перезаписывать их на каждый клик — просто медленнее без пользы. Для
    редкого случая ручной правки в старых данных Финтабло — отдельная
    кнопка "Полная синхронизация" (см. sync_fintablo_full)."""
    if request.method == "POST":
        try:
            created, updated, deleted_api, deleted_excel = sync_operations(incremental_sync_from())
        except FintabloError as e:
            messages.error(request, str(e))
        else:
            messages.success(
                request,
                f"Финтабло: создано {created}, обновлено {updated}, удалено {deleted_api}"
                + (f", заменено Excel-операций: {deleted_excel}" if deleted_excel else ""),
            )
    return redirect("upload")


@permission_required("finance.add_operation", raise_exception=True)
def sync_fintablo_full(request):
    """Полная синхронизация — с DEFAULT_SYNC_FROM (2026-01-01), а не от
    последней загруженной операции. Нужна, только если кто-то задним числом
    поправил старые данные прямо в Финтабло — обычная синхронизация (см.
    sync_fintablo_now) такие правки не увидит, раз не перечитывает старые
    даты заново."""
    if request.method == "POST":
        try:
            created, updated, deleted_api, deleted_excel = sync_operations(DEFAULT_SYNC_FROM)
        except FintabloError as e:
            messages.error(request, str(e))
        else:
            messages.success(
                request,
                f"Полная синхронизация Финтабло: создано {created}, обновлено {updated}, удалено {deleted_api}"
                + (f", заменено Excel-операций: {deleted_excel}" if deleted_excel else ""),
            )
    return redirect("upload")


BALANCE_FIELD_SEP = "␟"


@login_required
def fund_transfers(request):
    """Страница "Настройка остатков" — три независимых раздела: ручные
    перемещения денег между фондами (исходная функция страницы, см.
    FundTransfer/fund_balance.load_transfer_deltas), точки сверки остатка
    (FundBalanceSnapshot, см. set_fund_balance) и переопределение доли
    поступлений по месяцам (FundIncomeShare, см. set_fund_income_share) —
    каждый раздел сохраняется своей формой, эта функция только отдаёт GET
    и обрабатывает POST перемещения (как и раньше)."""
    if request.method == "POST":
        form = FundTransferForm(request.POST)
        if form.is_valid():
            FundTransfer.objects.create(
                date=form.cleaned_data["date"],
                from_fund=form.cleaned_data["from_fund"],
                to_fund=form.cleaned_data["to_fund"],
                amount=form.cleaned_data["amount"],
                note=form.cleaned_data["note"],
            )
            messages.success(request, "Перемещение сохранено.")
            return redirect("fund_transfers")
    else:
        form = FundTransferForm()
    transfers = FundTransfer.objects.all()
    all_funds = list(fund_balance.load_all_funds().keys())
    return render(request, "finance/fund_transfers.html", {
        "form": form, "transfers": transfers, "all_funds": all_funds,
        "balance_snapshots": FundBalanceSnapshot.objects.all(),
        "income_shares": FundIncomeShare.objects.all(),
        "distribution_start": fund_balance.DISTRIBUTION_START.isoformat(),
    })


@login_required
def fund_transfer_delete(request, pk):
    if request.method == "POST":
        FundTransfer.objects.filter(pk=pk).delete()
        messages.success(request, "Перемещение удалено.")
    return redirect("fund_transfers")


@login_required
def set_fund_balance(request):
    """Точка сверки остатка — "на конец даты date остаток фонда X равен
    сумме Y" (см. finance.models.FundBalanceSnapshot). Поля не по одному, а
    сразу на все фонды разом (balance␟<фонд>) — пустое поле у фонда просто
    пропускается, остальные не трогает (тот же приём динамических полей по
    списку фондов/категорий, что и в plan_save_year и т.п.)."""
    if request.method != "POST":
        return redirect("fund_transfers")

    raw_date = request.POST.get("date", "")
    try:
        snapshot_date = date.fromisoformat(raw_date)
    except ValueError:
        messages.error(request, "Некорректная дата.")
        return redirect("fund_transfers")
    if snapshot_date < fund_balance.DISTRIBUTION_START:
        messages.error(
            request,
            f"Остатки не считаются раньше {fund_balance.DISTRIBUTION_START:%d.%m.%Y} — точка сверки раньше этой даты не имеет смысла.",
        )
        return redirect("fund_transfers")

    all_funds = fund_balance.load_all_funds().keys()
    saved = 0
    for fund in all_funds:
        raw = request.POST.get(f"balance{BALANCE_FIELD_SEP}{fund}", "").strip()
        if not raw:
            continue
        FundBalanceSnapshot.objects.update_or_create(
            fund=fund, date=snapshot_date, defaults={"amount": _parse_money(raw)},
        )
        saved += 1
    if saved:
        messages.success(request, f"Остаток на {snapshot_date:%d.%m.%Y} сохранён для {saved} фонд(ов).")
    else:
        messages.error(request, "Ни для одного фонда не указана сумма.")
    return redirect("fund_transfers")


@login_required
def fund_balance_snapshot_delete(request, pk):
    if request.method == "POST":
        FundBalanceSnapshot.objects.filter(pk=pk).delete()
        messages.success(request, "Точка сверки удалена.")
    return redirect("fund_transfers")


@login_required
def set_fund_income_share(request):
    """Доля фонда от поступлений за конкретный месяц (см.
    finance.models.FundIncomeShare) — вводится в процентах, хранится долей
    (0..1). Тот же приём динамических полей на все фонды разом, что и в
    set_fund_balance; пустое поле у фонда — этот месяц для него не трогаем,
    остаётся действовать значение по умолчанию (см. fund_balance.resolve_share)."""
    if request.method != "POST":
        return redirect("fund_transfers")

    raw_month = request.POST.get("month", "")
    try:
        year_str, month_str = raw_month.split("-")
        year, month = int(year_str), int(month_str)
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        messages.error(request, "Некорректный месяц.")
        return redirect("fund_transfers")

    all_funds = fund_balance.load_all_funds().keys()
    saved = 0
    for fund in all_funds:
        raw = request.POST.get(f"share{BALANCE_FIELD_SEP}{fund}", "").strip()
        if not raw:
            continue
        FundIncomeShare.objects.update_or_create(
            fund=fund, year=year, month=month, defaults={"share": _parse_money(raw) / 100},
        )
        saved += 1
    if saved:
        messages.success(request, f"Распределение поступлений на {month:02d}.{year} сохранено для {saved} фонд(ов).")
    else:
        messages.error(request, "Ни для одного фонда не указан процент.")
    return redirect("fund_transfers")


@login_required
def fund_income_share_delete(request, pk):
    if request.method == "POST":
        FundIncomeShare.objects.filter(pk=pk).delete()
        messages.success(request, "Переопределение удалено.")
    return redirect("fund_transfers")


# --------------------------------------------------------------- общее

def _col_period(kind, year, key):
    """(start, end) периода, на который указывает ключ столбца."""
    if kind == "year":
        month = key
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if kind == "month":
        return key  # уже (start, end)
    return key, key  # week: key = сам день


def _default_year(classified):
    years = aggregate.years_present(classified) if classified else [date.today().year]
    return years[-1]


@login_required
def income_default(request):
    classified, _income_categories, _rows = services.load_classified_operations()
    return redirect("income_year", year=_default_year(classified))


@login_required
def expenses_default(request):
    classified, _income_categories, _rows = services.load_classified_operations()
    return redirect("expenses_year", year=_default_year(classified))


@login_required
def plan_default(request):
    classified, _income_categories, _rows = services.load_classified_operations()
    return redirect("plan_year", year=_default_year(classified))


@login_required
def income_plan_default(request):
    classified, _income_categories, _rows = services.load_classified_operations()
    return redirect("income_plan_year", year=_default_year(classified))


# --------------------------------------------------------------- поступления
#
# Подкатегорий у поступлений больше нет (см. finance.logic.classify) —
# каждая категория теперь ведёт себя как "группа" (цветная плашка,
# сворачивается), а внутри — необязательный разворот по "направлению",
# ровно как это сделано для расходов.

def _income_table(classified, income_categories, kind, year, month=None, week=None):
    if kind == "year":
        rows, columns, data = aggregate.year_table(classified, income_categories, year)
    elif kind == "month":
        rows, columns, data = aggregate.month_table(classified, income_categories, year, month)
    else:
        rows, columns, data = aggregate.week_table(classified, income_categories, week[0], week[1])

    col_meta = []
    for label, key in columns:
        start, end = _col_period(kind, year, key)
        nav_url = None
        if kind == "year":
            nav_url = reverse("income_month", args=[year, key])
            label = aggregate.MONTH_NAMES[key - 1]
        elif kind == "month":
            nav_url = reverse("income_week", args=[year, month, key[0].isoformat()])
            label = f"{key[0]:%d.%m}–{key[1]:%d.%m}"
        col_meta.append({"label": label, "nav_url": nav_url, "start": start.isoformat(), "end": end.isoformat()})

    categories_out = []
    col_totals = [0.0] * len(columns)
    for category, _subcategory in rows:
        cells = []
        row_total = 0.0
        for idx, (_label, key) in enumerate(columns):
            value = data[(category, None, key)]
            row_total += value
            col_totals[idx] += value
            cells.append({"value": value, "start": col_meta[idx]["start"], "end": col_meta[idx]["end"]})

        directions = aggregate.income_directions(classified, category)
        dir_rows = []
        if len(directions) > 1:
            for direction in directions:
                dir_cells = []
                dir_total = 0.0
                for idx, (_label, key) in enumerate(columns):
                    start, end = _col_period(kind, year, key)
                    value = aggregate.income_direction_period_total(classified, category, direction, start, end)
                    dir_cells.append({"value": value, "start": col_meta[idx]["start"], "end": col_meta[idx]["end"]})
                    dir_total += value
                dir_rows.append({"direction": direction, "cells": dir_cells, "total": dir_total})

        categories_out.append({
            "category": category, "cells": cells, "total": row_total, "directions": dir_rows,
        })

    return {
        "columns": col_meta,
        "categories": categories_out,
        "col_totals": col_totals,
        "grand_total": sum(col_totals),
    }


def _income_context(request, kind, year, month=None, week=None):
    classified, income_categories, _rows = services.load_classified_operations()
    ctx = _income_table(classified, income_categories, kind, year, month=month, week=week)
    ctx["kind"] = kind
    ctx["year"] = year
    if kind == "year":
        ctx["period_title"] = f"{year} год"
        ctx["home_url"] = None
        ctx["period_start"], ctx["period_end"] = date(year, 1, 1), date(year, 12, 31)
    elif kind == "month":
        ctx["period_title"] = f"{aggregate.MONTH_NAMES[month - 1]} {year}"
        ctx["home_url"] = reverse("income_year", args=[year])
        ctx["period_start"] = date(year, month, 1)
        ctx["period_end"] = date(year, month, calendar.monthrange(year, month)[1])
    else:
        ctx["period_title"] = (
            f"{aggregate.MONTH_NAMES[week[0].month - 1]}, {week[0]:%d.%m}–{week[1]:%d.%m.%Y}"
        )
        ctx["home_url"] = reverse("income_month", args=[year, week[0].month])
        ctx["period_start"], ctx["period_end"] = week
    return ctx


@login_required
def income_year(request, year):
    return render(request, "finance/income_grid.html", _income_context(request, "year", year))


@login_required
def income_month(request, year, month):
    return render(request, "finance/income_grid.html", _income_context(request, "month", year, month=month))


@login_required
def income_week(request, year, month, week_start):
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    return render(request, "finance/income_grid.html", _income_context(request, "week", year, month=month, week=(start, end)))


def _parse_income_ops_params(request):
    """direction=None значит "не фильтруем по направлению" (клик по строке
    категории целиком) — это не то же самое, что direction="" (клик по
    направлению "(без направления)"), поэтому различаем через has_direction,
    как и для расходов. category=None (параметр вообще отсутствует в
    запросе, а не просто пустой) значит "не фильтруем по категории" — так
    кликают по общей строке "Поступления (факт)" на вкладке "План", где
    разбивки по категориям нет."""
    src = request.POST if request.method == "POST" else request.GET
    category = src.get("category") if "category" in src else None
    direction = src.get("direction", "") if src.get("has_direction") else None
    start = date.fromisoformat(src.get("start"))
    end = date.fromisoformat(src.get("end"))
    return category, direction, start, end


def _income_matching(classified, category, direction, start, end):
    return [
        op for op in classified
        if start <= op["date"] <= end and op["amount"] > 0 and not op["excluded"]
        and (category is None or op["category"] == category)
        and (direction is None or op["direction"].strip() == direction)
    ]


def _income_ops_context(classified, category, direction, start, end):
    matching = _income_matching(classified, category, direction, start, end)
    return {
        "ops": matching, "category": category or "", "all_categories": category is None, "subcategory": "",
        "direction": direction or "", "has_direction": direction is not None,
        "start": start.isoformat(), "end": end.isoformat(),
        "kind": "income",
    }


@login_required
def income_ops(request):
    category, direction, start, end = _parse_income_ops_params(request)
    classified, _income_categories, _rows = services.load_classified_operations()
    ctx = _income_ops_context(classified, category, direction, start, end)
    return render(request, "finance/_ops_panel.html", ctx)


# --------------------------------------------------------------- расходы

def _expense_table(classified, rows, kind, year, month=None, week=None):
    if kind == "year":
        _rows, columns, data = aggregate.expense_year_table(classified, rows, year)
    elif kind == "month":
        _rows, columns, data = aggregate.expense_month_table(classified, rows, year, month)
    else:
        _rows, columns, data = aggregate.expense_week_table(classified, rows, week[0], week[1])

    col_meta = []
    for label, key in columns:
        start, end = _col_period(kind, year, key)
        nav_url = None
        if kind == "year":
            nav_url = reverse("expenses_month", args=[year, key])
            label = aggregate.MONTH_NAMES[key - 1]
        elif kind == "month":
            nav_url = reverse("expenses_week", args=[year, month, key[0].isoformat()])
            label = f"{key[0]:%d.%m}–{key[1]:%d.%m}"
        col_meta.append({"label": label, "nav_url": nav_url, "start": start.isoformat(), "end": end.isoformat()})

    groups = []
    current_group = None
    group_ref = None
    col_totals = [0.0] * len(columns)
    for group, category, subcategory in rows:
        if group != current_group:
            current_group = group
            group_ref = {
                "name": group, "rows": [], "col_totals": [0.0] * len(columns),
            }
            groups.append(group_ref)

        directions = aggregate.expense_directions(classified, group, category, subcategory)
        row_cells = []
        row_total = 0.0
        for idx, (_label, key) in enumerate(columns):
            value = data[(group, category, subcategory, key)]
            row_total += value
            col_totals[idx] += value
            group_ref["col_totals"][idx] += value
            row_cells.append({"value": value, "start": col_meta[idx]["start"], "end": col_meta[idx]["end"]})

        dir_rows = []
        if len(directions) > 1:
            for direction in directions:
                dir_cells = []
                dir_total = 0.0
                for idx, (_label, key) in enumerate(columns):
                    start, end = _col_period(kind, year, key)
                    value = aggregate.expense_direction_period_total(classified, group, category, subcategory, direction, start, end)
                    dir_cells.append({"value": value, "start": col_meta[idx]["start"], "end": col_meta[idx]["end"]})
                    dir_total += value
                dir_rows.append({"direction": direction, "cells": dir_cells, "total": dir_total})

        group_ref["rows"].append({
            "category": category, "subcategory": subcategory, "cells": row_cells, "total": row_total,
            "directions": dir_rows,
        })

    for group_ref in groups:
        group_ref["grand_total"] = sum(group_ref["col_totals"])

    return {
        "columns": col_meta,
        "groups": groups,
        "col_totals": col_totals,
        "grand_total": sum(col_totals),
    }


def _expenses_context(request, kind, year, month=None, week=None):
    classified, _income_categories, rows = services.load_classified_operations()
    ctx = _expense_table(classified, rows, kind, year, month=month, week=week)
    ctx["kind"] = kind
    ctx["year"] = year
    if kind == "year":
        ctx["period_title"] = f"{year} год"
        ctx["home_url"] = None
        ctx["period_start"], ctx["period_end"] = date(year, 1, 1), date(year, 12, 31)
    elif kind == "month":
        ctx["period_title"] = f"{aggregate.MONTH_NAMES[month - 1]} {year}"
        ctx["home_url"] = reverse("expenses_year", args=[year])
        ctx["period_start"] = date(year, month, 1)
        ctx["period_end"] = date(year, month, calendar.monthrange(year, month)[1])
    else:
        ctx["period_title"] = (
            f"{aggregate.MONTH_NAMES[week[0].month - 1]}, {week[0]:%d.%m}–{week[1]:%d.%m.%Y}"
        )
        ctx["home_url"] = reverse("expenses_month", args=[year, week[0].month])
        ctx["period_start"], ctx["period_end"] = week
    return ctx


@login_required
def expenses_year(request, year):
    return render(request, "finance/expenses_grid.html", _expenses_context(request, "year", year))


@login_required
def expenses_month(request, year, month):
    return render(request, "finance/expenses_grid.html", _expenses_context(request, "month", year, month=month))


@login_required
def expenses_week(request, year, month, week_start):
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    return render(request, "finance/expenses_grid.html", _expenses_context(request, "week", year, month=month, week=(start, end)))


def _expense_matching(classified, group, category, subcategory, direction, start, end):
    return [
        op for op in classified
        if start <= op["date"] <= end and op["amount"] < 0 and not op["excluded"]
        and (group is None or op["exp_group"] == group)
        and (category is None or op["exp_category"] == category)
        and op["exp_subcategory"] == subcategory and (direction is None or op["direction"].strip() == direction)
    ]


def _parse_expense_ops_params(request):
    """direction=None значит "не фильтруем по направлению" (клик по строке
    категории целиком) — это НЕ то же самое, что direction="" (клик по
    направлению "(без направления)"), поэтому различаем их через отдельный
    флаг has_direction, а не только по пустоте строки. group/category=None
    (параметр вообще отсутствует в запросе) значит "не фильтруем" — так
    кликают по общей строке "Факт расходы (всего)" на вкладке "План"."""
    src = request.POST if request.method == "POST" else request.GET
    group = src.get("group") if "group" in src else None
    category = src.get("category") if "category" in src else None
    subcategory = src.get("subcategory") or None
    direction = src.get("direction", "") if src.get("has_direction") else None
    start = date.fromisoformat(src.get("start"))
    end = date.fromisoformat(src.get("end"))
    return group, category, subcategory, direction, start, end


def _expense_ops_context(classified, group, category, subcategory, direction, start, end):
    matching = _expense_matching(classified, group, category, subcategory, direction, start, end)
    return {
        "ops": matching, "group": group or "", "all_groups": group is None,
        "category": category or "", "all_categories": category is None, "subcategory": subcategory or "",
        "direction": direction or "", "has_direction": direction is not None,
        "start": start.isoformat(), "end": end.isoformat(),
        "kind": "expenses",
    }


@login_required
def expenses_ops(request):
    group, category, subcategory, direction, start, end = _parse_expense_ops_params(request)
    classified, _income_categories, _rows = services.load_classified_operations()
    ctx = _expense_ops_context(classified, group, category, subcategory, direction, start, end)
    return render(request, "finance/_ops_panel.html", ctx)


# --------------------------------------------------------------- план

PLAN_FIELD_SEP = "␟"
PLAN_FIELD_PREFIX = f"plan{PLAN_FIELD_SEP}"
PLANMONTH_PREFIX = f"planmonth{PLAN_FIELD_SEP}"
PLANWEEK_PREFIX = f"planweek{PLAN_FIELD_SEP}"
PLANDAY_PREFIX = f"planday{PLAN_FIELD_SEP}"

# EMPTY_DIRECTION_KEY/_dir_key живут в income_plan.py (см. комментарий там)
# — используются и для плана расходов, и для плана поступлений, чтобы
# "категория/группа целиком" и реальное направление "(без направления)" не
# делили один и тот же ключ хранения.
EMPTY_DIRECTION_KEY = income_plan.EMPTY_DIRECTION_KEY
_dir_key = income_plan.dir_key


def plan_field_name(group, category, subcategory, direction):
    return PLAN_FIELD_SEP.join(["plan", group, category, subcategory or "", _dir_key(direction)])


def expense_week_field_name(group, category, subcategory, direction, week_start):
    """Недельный план, правится прямо в месячном виде (см. income_plan_field_name,
    та же идея — здесь неделя, там месяц/день)."""
    return PLAN_FIELD_SEP.join(["planweek", group, category, subcategory or "", _dir_key(direction), week_start.isoformat()])


def expense_daily_field_name(group, category, subcategory, direction, day):
    """Дневной план, правится в дневном виде — независимо от недельного,
    см. IncomeDailyPlan/income_plan_field_name."""
    return PLAN_FIELD_SEP.join(["planday", group, category, subcategory or "", _dir_key(direction), day.isoformat()])


def expense_monthly_field_name(group, category, subcategory, direction, month):
    """Месячный план, правится в годовом виде (см. PLAN.md, "новая логика
    планирования расходов") — каскадом делится по неделям, см.
    finance.expense_plan.apply_monthly_plan."""
    return PLAN_FIELD_SEP.join(["planmonth", group, category, subcategory or "", _dir_key(direction), str(month)])


def _plan_col_value(plan_map, group, category, subcategory, direction, week_start):
    # subcategory приходит как Python None (см. expenses.expense_rows — у
    # расходов подкатегорий больше нет), а plan_map хранит ключи с "" (так
    # его пишет _load_plan_map/plan_save*) — без нормализации None != "" и
    # словарь никогда не находил сохранённое значение, тихо возвращая 0
    # (то самое "сохранила — и всё стёрлось", хотя в базе всё было верно).
    sub_key = subcategory or ""
    dir_key = _dir_key(direction)
    return plan_map.get((group, category, sub_key, dir_key, week_start), 0.0)


def _load_plan_map():
    return {
        (p.group, p.category, p.subcategory or "", p.direction or "", p.week_start): float(p.amount)
        for p in PlanEntry.objects.all()
    }


def _load_expense_monthly_map(year):
    # Аналог _load_plan_map, но для ExpenseMonthlyPlan (годовой вид) — без
    # него expense_plan.get_monthly_plan() дёргал отдельный SELECT на
    # КАЖДУЮ ячейку годовой сетки (категория x направление x месяц —
    # тысячи вызовов на один рендер, львиная доля медленной загрузки
    # годового плана, см. обсуждение с пользователем).
    return {
        (p.group, p.category, p.subcategory or "", p.direction or "", p.month): float(p.amount)
        for p in ExpenseMonthlyPlan.objects.filter(year=year)
    }


def _month_has_weekly_data_cached(plan_map, group, category, sub_key, dir_key, year, month):
    # Заменяет expense_plan.month_has_weekly_data — та же логика (есть ли у
    # недель этого месяца свой ненулевой план), но по уже загруженному
    # в память plan_map (см. _load_plan_map), а не отдельным запросом в БД
    # на каждую ячейку.
    for week_start, _week_end in aggregate.month_weeks(year, month):
        if plan_map.get((group, category, sub_key, dir_key, week_start), 0.0):
            return True
    return False


def _load_expense_daily_map(start, end=None):
    if end is None:
        end = start + timedelta(days=6)
    return {
        (p.group, p.category, p.subcategory or "", p.direction or "", p.day): float(p.amount)
        for p in ExpenseDailyPlan.objects.filter(day__gte=start, day__lte=end)
    }


def _week_has_daily_data_cached(daily_map, group, category, sub_key, dir_key, week_start):
    # Заменяет expense_plan.week_has_daily_data — та же логика (есть ли у
    # недели уже свои дневные данные), но по уже загруженному в память
    # daily_map, а не отдельным EXISTS-запросом на каждую ячейку месячного
    # вида (на весь месяц — сотни направлений x недель, это и было основной
    # причиной медленной загрузки месячного плана).
    for i in range(7):
        if (group, category, sub_key, dir_key, week_start + timedelta(days=i)) in daily_map:
            return True
    return False


def _expense_day_cells(daily_map, group, category, subcategory, direction, week_start, weekly_total):
    """[{value, field_name}, ...] на 7 дней недели — дневной override, если
    его правили отдельно.

    Для дня БЕЗ своего значения: если у этой строки (группа/категория/
    подкатегория/направление) в этой неделе уже есть ХОТЯ БЫ один явно
    заданный день — 0, а не "неделя/7". Иначе (неделю ещё не разворачивали
    по дням вообще) — "неделя/7", как разумное стартовое значение. Без
    этого разделения после первой же правки одного дня у ВСЕХ остальных,
    ещё не тронутых дней "из ниоткуда" менялся плейсхолдер (недельная сумма
    выросла — вырос и "неделя/7"), хотя реально в базе для них ничего не
    сохранено; ровно так уже устроен plan_projection.planned_daily_fund_expense
    (см. её докстринг) — эта функция просто отражала старую, рассинхронизированную
    с ним логику."""
    dir_key = _dir_key(direction)
    has_daily_data = any(
        (group, category, subcategory or "", dir_key, week_start + timedelta(days=i)) in daily_map
        for i in range(7)
    )
    default_v = 0.0 if has_daily_data else (weekly_total or 0.0) / 7.0
    out = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        override = daily_map.get((group, category, subcategory or "", dir_key, d))
        value = override if override is not None else default_v
        out.append({"value": value, "field_name": expense_daily_field_name(group, category, subcategory, direction, d), "editable": True})
    return out


def _sum_days(daily_dict, start, end):
    total = 0.0
    day = start
    while day <= end:
        total += daily_dict.get(day, 0.0)
        day += timedelta(days=1)
    return total


def _plan_vs_fact_ok(fact_cells, plan_cells, settled_col, is_income):
    """[True/False/None, ...] — сравнение факта с планом по каждому столбцу
    (см. PLAN.md: поступления — план выполнен, если факт >= план; расходы —
    план выполнен, если факт <= план). None — если столбец ещё не settled
    (период не закончился, до 30.08.2026, или план для этого столбца не
    ведётся вообще — например по дням у расходов)."""
    if plan_cells is None:
        plan_cells = [None] * len(fact_cells)
    out = []
    for fact, plan, settled in zip(fact_cells, plan_cells, settled_col):
        if not settled or plan is None:
            out.append(None)
        else:
            out.append(fact >= plan if is_income else fact <= plan)
    return out


def _plan_context(request, kind, year, month=None, week=None):
    classified, income_categories, rows = services.load_classified_operations()
    plan_map = _load_plan_map()
    monthly_plan_map = _load_expense_monthly_map(year)
    fund_balances = expenses.load_fund_balances()
    weekend_flags = income_plan.load_weekend_flags()
    # Переопределения доли поступлений по месяцам и ручные точки сверки
    # остатка — см. страницу "Настройка остатков" (fund_transfers) и
    # fund_balance.resolve_share/load_balance_snapshots.
    income_shares_map = fund_balance.load_income_shares()
    balance_snapshots = fund_balance.load_balance_snapshots()
    # Один запрос на весь план поступлений вместо SELECT на каждую ячейку —
    # см. income_plan.load_weekly_plan_map, используется и в проекции
    # остатка (planned_daily_income/planned_daily_balances ниже), и во
    # встроенном блоке "Планирование поступлений" (_income_plan_table_*).
    income_weekly_plan_map = income_plan.load_weekly_plan_map()
    income_monthly_plan_map = income_plan.load_monthly_plan_map()
    income_daily_plan_map = income_plan.load_daily_plan_map()

    if kind == "year":
        _rows, columns, data = aggregate.expense_year_table(classified, rows, year)
    elif kind == "month":
        _rows, columns, data = aggregate.expense_month_table(classified, rows, year, month)
    else:
        _rows, columns, data = aggregate.expense_week_table(classified, rows, week[0], week[1])

    period_ranges = [_col_period(kind, year, key) for _label, key in columns]
    income_values = [aggregate.total_income_for_period(classified, s, e) for s, e in period_ranges]

    # Плановые поступления и остаток-по-плану (см. PLAN.md, строка 136) —
    # день за днём с 30.08.2026, вперёд до конца видимого периода (это
    # прогноз, в отличие от фактического остатка его не ограничивают
    # последней датой с операциями).
    latest_op_date = max((op["date"] for op in classified), default=date.today())
    period_end_overall = period_ranges[-1][1] if period_ranges else date.today()
    full_daily_planned_income = plan_projection.planned_daily_income(
        classified, income_categories, weekend_flags,
        period_ranges[0][0] if period_ranges else date.today(), period_end_overall,
        weekly_plan_map=income_weekly_plan_map, daily_plan_map=income_daily_plan_map,
    )
    planned_income_values = [_sum_days(full_daily_planned_income, s, e) for s, e in period_ranges]
    settled_col = [fund_balance.DISTRIBUTION_START <= end <= latest_op_date for _s, end in period_ranges]

    col_meta = []
    for (label, key), (start, end) in zip(columns, period_ranges):
        nav_url = None
        if kind == "year":
            nav_url = reverse("plan_month", args=[year, key])
            label = aggregate.MONTH_NAMES[key - 1]
        elif kind == "month":
            nav_url = reverse("plan_week", args=[year, month, key[0].isoformat()])
            label = f"{key[0]:%d.%m}–{key[1]:%d.%m}"
        col_meta.append({"label": label, "nav_url": nav_url, "start": start.isoformat(), "end": end.isoformat()})

    week_start_for_input = week[0] if kind == "week" else None
    daily_map = _load_expense_daily_map(week_start_for_input) if kind == "week" else None
    # Один запрос на весь месяц вместо EXISTS на каждую ячейку недели — см.
    # _week_has_daily_data_cached, используется ниже вместо
    # expense_plan.week_has_daily_data в цикле по неделям месяца.
    month_daily_map = None
    if kind == "month":
        week_starts = [key[0] for _label, key in columns]
        week_ends = [key[1] for _label, key in columns]
        month_daily_map = _load_expense_daily_map(min(week_starts), max(week_ends))

    groups = []
    current_group = None
    group_ref = None
    grand_fact = [0.0] * len(columns)
    grand_plan = [0.0] * len(columns)
    for group, category, subcategory in rows:
        if group != current_group:
            current_group = group
            # Доля может отличаться по месяцам (см. FundIncomeShare) —
            # поэтому считается отдельно на каждую колонку (в годовом виде
            # колонка = месяц, в остальных — все колонки внутри одного
            # месяца страницы). income_share_pct — одно число для заголовка,
            # только когда оно однозначно (не в годовом виде, см. шаблон).
            share_cols = [
                fund_balance.resolve_share(group, start.year, start.month, fund_balances, income_shares_map)
                for start, _end in period_ranges
            ]
            group_ref = {
                "name": group, "rows": [],
                "income_row": [v * s for v, s in zip(income_values, share_cols)],
                "income_share_pct": None if kind == "year" else (share_cols[0] * 100 if share_cols else None),
                "planned_income_row": [v * s for v, s in zip(planned_income_values, share_cols)],
                "plan_row": [0.0] * len(columns),
                "fact_row": [0.0] * len(columns),
            }
            groups.append(group_ref)

        fact_cells = [data[(group, category, subcategory, key)] for _label, key in columns]
        sub_key = subcategory or ""

        directions = aggregate.expense_directions(classified, group, category, subcategory)
        expandable = len(directions) > 1

        dir_rows = []
        if expandable:
            for direction in directions:
                dir_key = _dir_key(direction)
                d_fact_cells = []
                for start, end in period_ranges:
                    d_fact_cells.append(aggregate.expense_direction_period_total(classified, group, category, subcategory, direction, start, end))

                if kind == "year":
                    d_plan_cells = [monthly_plan_map.get((group, category, sub_key, dir_key, key), 0.0) for _label, key in columns]
                elif kind == "month":
                    d_plan_cells = [_plan_col_value(plan_map, group, category, subcategory, direction, key[0]) for _label, key in columns]
                else:
                    d_plan_cells = [None] * len(columns)

                if kind == "week":
                    d_plan_total = plan_map.get((group, category, sub_key, dir_key, week_start_for_input), 0.0)
                    d_day_cells = _expense_day_cells(daily_map, group, category, subcategory, direction, week_start_for_input, d_plan_total)
                    d_edited_total = sum(c["value"] or 0.0 for c in d_day_cells)
                    d_plan_edit_cells = None
                elif kind == "month":
                    d_plan_total = sum(d_plan_cells)
                    d_day_cells = None
                    d_edited_total = d_plan_total
                    d_plan_edit_cells = [
                        {
                            "value": d_plan_cells[idx],
                            "field_name": expense_week_field_name(group, category, subcategory, direction, key[0]),
                            "has_lower": _week_has_daily_data_cached(month_daily_map, group, category, sub_key, dir_key, key[0]),
                        }
                        for idx, (_label, key) in enumerate(columns)
                    ]
                else:
                    d_plan_total = sum(d_plan_cells)
                    d_day_cells = None
                    d_edited_total = None
                    d_plan_edit_cells = [
                        {
                            "value": d_plan_cells[idx],
                            "field_name": expense_monthly_field_name(group, category, subcategory, direction, key),
                            "has_lower": _month_has_weekly_data_cached(plan_map, group, category, sub_key, dir_key, year, key),
                        }
                        for idx, (_label, key) in enumerate(columns)
                    ]
                dir_rows.append({
                    "direction": direction, "fact_cells": d_fact_cells, "fact_total": sum(d_fact_cells),
                    "plan_cells": d_plan_cells, "plan_total": d_plan_total, "plan_edit_cells": d_plan_edit_cells,
                    "field_name": plan_field_name(group, category, subcategory, direction),
                    "fact_ok": _plan_vs_fact_ok(d_fact_cells, d_plan_cells, settled_col, False),
                    "day_cells": d_day_cells, "current_total": d_plan_total, "edited_total": d_edited_total,
                })

        # Ячейки САМОЙ категории — правятся напрямую, ПОКА по направлениям
        # за этот же столбец (месяц/неделю/день) ничего не ввели. Как
        # только у направлений появляется своя ненулевая сумма за этот
        # столбец — именно этот столбец категории блокируется и показывает
        # сумму направлений (остальные столбцы той же категории остаются
        # редактируемыми, если по ним направления ещё пустые). То есть
        # блокировка — по каждому столбцу отдельно, а не по всей строке
        # сразу: можно вести план либо категорией целиком, либо по
        # направлениям, смешивая по разным периодам.
        if kind == "week":
            stored_plan_total = plan_map.get((group, category, sub_key, "", week_start_for_input), 0.0)
            day_cells = []
            for idx in range(7):
                dir_sum = sum((dr["day_cells"][idx]["value"] or 0.0) for dr in dir_rows) if expandable else 0.0
                if dir_sum:
                    day_cells.append({
                        "value": dir_sum, "editable": False,
                        "field_name": expense_daily_field_name(group, category, subcategory, None, week_start_for_input + timedelta(days=idx)),
                    })
                else:
                    default_cells = _expense_day_cells(daily_map, group, category, subcategory, None, week_start_for_input, stored_plan_total)
                    day_cells.append(default_cells[idx])
            plan_total = stored_plan_total
            edited_total = sum(c["value"] or 0.0 for c in day_cells)
            plan_cells = [None] * len(columns)
            plan_edit_cells = None
        elif kind == "month":
            plan_cells = []
            plan_edit_cells = []
            for idx, (_label, key) in enumerate(columns):
                dir_sum = sum((dr["plan_cells"][idx] or 0.0) for dr in dir_rows) if expandable else 0.0
                if dir_sum:
                    plan_cells.append(dir_sum)
                    plan_edit_cells.append({
                        "value": dir_sum, "editable": False,
                        "field_name": expense_week_field_name(group, category, subcategory, None, key[0]),
                    })
                else:
                    value = _plan_col_value(plan_map, group, category, subcategory, None, key[0])
                    plan_cells.append(value)
                    plan_edit_cells.append({
                        "value": value, "editable": True,
                        "field_name": expense_week_field_name(group, category, subcategory, None, key[0]),
                        "has_lower": _week_has_daily_data_cached(month_daily_map, group, category, sub_key, "", key[0]),
                    })
            plan_total = sum(plan_cells)
            day_cells = None
            edited_total = plan_total
        else:  # year
            plan_cells = []
            plan_edit_cells = []
            for idx, (_label, key) in enumerate(columns):
                dir_sum = sum((dr["plan_cells"][idx] or 0.0) for dr in dir_rows) if expandable else 0.0
                if dir_sum:
                    plan_cells.append(dir_sum)
                    plan_edit_cells.append({
                        "value": dir_sum, "editable": False,
                        "field_name": expense_monthly_field_name(group, category, subcategory, None, key),
                    })
                else:
                    value = monthly_plan_map.get((group, category, sub_key, "", key), 0.0)
                    plan_cells.append(value)
                    plan_edit_cells.append({
                        "value": value, "editable": True,
                        "field_name": expense_monthly_field_name(group, category, subcategory, None, key),
                        "has_lower": _month_has_weekly_data_cached(plan_map, group, category, sub_key, "", year, key),
                    })
            plan_total = sum(plan_cells)
            day_cells = None
            edited_total = None

        for idx in range(len(columns)):
            group_ref["fact_row"][idx] += fact_cells[idx]
            grand_fact[idx] += fact_cells[idx]
            if kind != "week":
                group_ref["plan_row"][idx] += plan_cells[idx] or 0.0
                grand_plan[idx] += plan_cells[idx] or 0.0

        group_ref["rows"].append({
            "category": category, "subcategory": subcategory,
            "fact_cells": fact_cells, "fact_total": sum(fact_cells),
            "plan_cells": plan_cells, "plan_total": plan_total, "plan_edit_cells": plan_edit_cells,
            "directions": dir_rows, "expandable": expandable,
            "field_name": plan_field_name(group, category, subcategory, None),
            "fact_ok": _plan_vs_fact_ok(fact_cells, plan_cells, settled_col, False),
            "day_cells": day_cells, "current_total": plan_total, "edited_total": edited_total,
        })

    # "Копилка (резерв)" получает долю поступлений, но на неё никогда не
    # классифицируют расходы — своей строки в rows у неё в принципе не
    # бывает. Показываем её отдельной группой с пустым списком статей, а не
    # молча выкидываем из остатка.
    existing_names = {g["name"] for g in groups}
    for fund_name, info in fund_balances.items():
        if fund_name not in existing_names:
            share_cols = [
                fund_balance.resolve_share(fund_name, start.year, start.month, fund_balances, income_shares_map)
                for start, _end in period_ranges
            ]
            groups.append({
                "name": fund_name, "rows": [],
                "income_row": [v * s for v, s in zip(income_values, share_cols)],
                "income_share_pct": None if kind == "year" else (share_cols[0] * 100 if share_cols else None),
                "planned_income_row": [v * s for v, s in zip(planned_income_values, share_cols)],
                "plan_row": [0.0] * len(columns),
                "fact_row": [0.0] * len(columns),
            })

    # "Фонд чистой прибыли" реально классифицирует расходы (попадает в
    # groups по ходу основного цикла), а "Копилка (резерв)" — только
    # синтетически (см. выше), поэтому порядок групп после обоих циклов не
    # гарантированно совпадает с заданным порядком фондов — сортируем явно.
    groups.sort(key=lambda g: expenses.FUND_ORDER.index(g["name"]) if g["name"] in expenses.FUND_ORDER else len(expenses.FUND_ORDER))

    for group_ref in groups:
        group_ref["income_total"] = sum(group_ref["income_row"])
        group_ref["planned_income_total"] = sum(group_ref["planned_income_row"])
        group_ref["fact_total"] = sum(group_ref["fact_row"])
        group_ref["income_ok"] = _plan_vs_fact_ok(group_ref["income_row"], group_ref["planned_income_row"], settled_col, True)

    # Остаток — фактический, день за днём с 30.08.2026 (см. finance.fund_balance).
    # Считаем только до последней даты, где вообще есть операции — дальше
    # это было бы "прогнозом", а не фактом (latest_op_date/period_end_overall
    # уже посчитаны выше, для плановых поступлений).
    all_funds = fund_balance.load_all_funds()
    balances_up_to = min(period_end_overall, latest_op_date)
    transfer_deltas = fund_balance.load_transfer_deltas()
    daily_balances = fund_balance.actual_daily_balances(
        classified, all_funds, balances_up_to, transfer_deltas,
        shares_map=income_shares_map, balance_snapshots=balance_snapshots,
    )

    for group_ref in groups:
        fund = group_ref["name"]
        balance_row = []
        for start, end in period_ranges:
            if end < fund_balance.DISTRIBUTION_START or start > latest_op_date:
                # период целиком до старта распределения, или целиком в
                # будущем — остатка ещё/уже не посчитать
                balance_row.append({"value": None, "partial": False})
            elif end <= latest_op_date:
                balance_row.append({"value": fund_balance.balance_at(daily_balances, fund, end), "partial": False})
            else:
                # период ещё не закончился, но частично уже прошёл — остаток
                # на последний известный день, серым (см. кол-во "partial")
                balance_row.append({"value": fund_balance.balance_at(daily_balances, fund, latest_op_date), "partial": True})
        group_ref["balance_row"] = balance_row
        last_known = next((c for c in reversed(balance_row) if c["value"] is not None), None)
        group_ref["balance_total"] = last_known or {"value": None, "partial": False}

        # остаток на начало периода — день перед первым столбцом
        if period_ranges:
            start0 = period_ranges[0][0]
            if start0 == fund_balance.DISTRIBUTION_START:
                group_ref["balance_start"] = all_funds.get(fund, {}).get("balance")
            elif start0 > fund_balance.DISTRIBUTION_START:
                prev_day = start0 - timedelta(days=1)
                group_ref["balance_start"] = (
                    fund_balance.balance_at(daily_balances, fund, prev_day) if prev_day <= latest_op_date else None
                )
            else:
                group_ref["balance_start"] = None
        else:
            group_ref["balance_start"] = None

    grand_balance_row = []
    for idx in range(len(columns)):
        values = [g["balance_row"][idx]["value"] for g in groups if g["balance_row"][idx]["value"] is not None]
        partial = any(g["balance_row"][idx]["partial"] for g in groups)
        grand_balance_row.append({"value": sum(values) if values else None, "partial": partial})
    grand_balance_total = next((c for c in reversed(grand_balance_row) if c["value"] is not None), None) or {"value": None, "partial": False}
    grand_balance_start_values = [g["balance_start"] for g in groups if g["balance_start"] is not None]
    grand_balance_start = sum(grand_balance_start_values) if grand_balance_start_values else None

    if kind == "week":
        for group_ref in groups:
            group_ref["plan_row"] = None  # по дням план не показываем
            group_ref["plan_total"] = sum(
                plan_map.get((group_ref["name"], r["category"], r["subcategory"] or "", "", week_start_for_input), 0.0)
                for r in group_ref["rows"]
            )
        grand_plan_total = sum(g["plan_total"] for g in groups)
    else:
        for group_ref in groups:
            group_ref["plan_total"] = sum(group_ref["plan_row"])
        grand_plan_total = sum(grand_plan)

    for group_ref in groups:
        group_ref["fact_ok"] = _plan_vs_fact_ok(group_ref["fact_row"], group_ref["plan_row"], settled_col, False)

    grand_income_ok = _plan_vs_fact_ok(income_values, planned_income_values, settled_col, True)
    grand_fact_ok = _plan_vs_fact_ok(grand_fact, grand_plan if kind != "week" else None, settled_col, False)

    # Плановый остаток — та же механика, что и фактический (см. выше), но
    # на плановых цифрах (finance.plan_projection) и без ограничения
    # последней датой факта — это прогноз, ему можно "заглядывать вперёд".
    planned_daily_balances = plan_projection.planned_daily_balances(
        classified, income_categories, weekend_flags, all_funds, period_end_overall, transfer_deltas,
        weekly_plan_map=income_weekly_plan_map, shares_map=income_shares_map, daily_plan_map=income_daily_plan_map,
    )
    for group_ref in groups:
        fund = group_ref["name"]
        planned_balance_row = []
        for _start, end in period_ranges:
            if end < fund_balance.DISTRIBUTION_START:
                planned_balance_row.append(None)
            else:
                planned_balance_row.append(plan_projection.balance_at(planned_daily_balances, fund, end))
        group_ref["planned_balance_row"] = planned_balance_row
        last_known_planned = next((v for v in reversed(planned_balance_row) if v is not None), None)
        group_ref["planned_balance_total"] = last_known_planned

        if period_ranges:
            start0 = period_ranges[0][0]
            if start0 == fund_balance.DISTRIBUTION_START:
                group_ref["planned_balance_start"] = all_funds.get(fund, {}).get("balance")
            elif start0 > fund_balance.DISTRIBUTION_START:
                prev_day = start0 - timedelta(days=1)
                group_ref["planned_balance_start"] = plan_projection.balance_at(planned_daily_balances, fund, prev_day)
            else:
                group_ref["planned_balance_start"] = None
        else:
            group_ref["planned_balance_start"] = None

    grand_planned_balance_row = []
    for idx in range(len(columns)):
        values = [g["planned_balance_row"][idx] for g in groups if g["planned_balance_row"][idx] is not None]
        grand_planned_balance_row.append(sum(values) if values else None)
    grand_planned_balance_total = next((v for v in reversed(grand_planned_balance_row) if v is not None), None)
    grand_planned_balance_start_values = [g["planned_balance_start"] for g in groups if g["planned_balance_start"] is not None]
    grand_planned_balance_start = sum(grand_planned_balance_start_values) if grand_planned_balance_start_values else None

    # "Планирование поступлений" встроено прямо сюда — строка "Плановые
    # поступления" разворачивается в ту же таблицу по категориям/
    # направлениям, что и раньше была на отдельной странице (см.
    # _income_plan_table_year/_month/_week, вынесенные из income_plan_*).
    # nav_url в колонках не нужен — переход по периодам уже даёт сама
    # страница "План", дублировать его тут незачем.
    if kind == "year":
        income_plan_result = _income_plan_table_year(
            classified, income_categories, weekend_flags, year, with_nav=False, monthly_plan_map=income_monthly_plan_map,
        )
    elif kind == "month":
        income_plan_result = _income_plan_table_month(
            classified, income_categories, weekend_flags, year, month, with_nav=False, weekly_plan_map=income_weekly_plan_map,
        )
    else:
        income_plan_result = _income_plan_table_week(classified, income_categories, weekend_flags, week[0])

    ctx = {
        "kind": kind, "year": year, "month": month, "columns": col_meta, "groups": groups,
        "grand_fact": grand_fact, "grand_fact_total": sum(grand_fact), "grand_fact_ok": grand_fact_ok,
        "grand_plan": grand_plan if kind != "week" else None, "grand_plan_total": grand_plan_total,
        "income_values": income_values, "income_total": sum(income_values), "grand_income_ok": grand_income_ok,
        "planned_income_values": planned_income_values, "planned_income_total": sum(planned_income_values),
        "grand_balance_row": grand_balance_row, "grand_balance_total": grand_balance_total,
        "grand_balance_start": grand_balance_start,
        "grand_planned_balance_row": grand_planned_balance_row, "grand_planned_balance_total": grand_planned_balance_total,
        "grand_planned_balance_start": grand_planned_balance_start,
        "week_start_for_input": week_start_for_input.isoformat() if week_start_for_input else None,
        "income_plan_columns": income_plan_result["columns"], "income_plan_table": income_plan_result["table"],
        "income_plan_col_totals": income_plan_result["col_totals"], "income_plan_grand_total": income_plan_result["grand_total"],
        "total_columns": len(col_meta) + 2 + (3 if kind == "week" else 1),
    }
    if kind == "year":
        ctx["period_title"] = f"{year} год"
        ctx["home_url"] = None
        ctx["save_url"] = reverse("plan_save_year", args=[year])
    elif kind == "month":
        ctx["period_title"] = f"{aggregate.MONTH_NAMES[month - 1]} {year}"
        ctx["home_url"] = reverse("plan_year", args=[year])
        ctx["save_url"] = reverse("plan_save_month", args=[year, month])
    else:
        ctx["period_title"] = f"{aggregate.MONTH_NAMES[week[0].month - 1]}, {week[0]:%d.%m}–{week[1]:%d.%m.%Y}"
        ctx["home_url"] = reverse("plan_month", args=[year, week[0].month])
        ctx["save_url"] = reverse("plan_save", args=[week_start_for_input.isoformat()])
    return ctx


@login_required
def plan_year(request, year):
    return render(request, "finance/plan_grid.html", _plan_context(request, "year", year))


@login_required
def plan_month(request, year, month):
    return render(request, "finance/plan_grid.html", _plan_context(request, "month", year, month=month))


@login_required
def plan_week(request, year, month, week_start):
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    return render(request, "finance/plan_grid.html", _plan_context(request, "week", year, month=month, week=(start, end)))


def _plan_export_response(ctx, filename):
    wb = plan_export.build_plan_workbook(ctx)
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@login_required
def plan_export_year(request, year):
    return _plan_export_response(_plan_context(request, "year", year), f"plan-{year}.xlsx")


@login_required
def plan_export_month(request, year, month):
    return _plan_export_response(_plan_context(request, "month", year, month=month), f"plan-{year}-{month:02d}.xlsx")


@login_required
def plan_export_week(request, year, month, week_start):
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    ctx = _plan_context(request, "week", year, month=month, week=(start, end))
    return _plan_export_response(ctx, f"plan-{start.isoformat()}.xlsx")


@login_required
def plan_save_year(request, year):
    """Год: план поступлений (встроенный блок) + месячный план расходов на
    группу/категорию (см. PLAN.md, "новая логика планирования расходов") —
    каскадом делится по неделям. Трогаем расходами только то, что реально
    изменилось (иначе при каждом сохранении страницы стирали бы вручную
    поправленные недели у нетронутых категорий) — подтверждение перезаписи
    уже спросили на клиенте, если внизу были свои данные (см. plan_grid.html)."""
    if request.method != "POST":
        return redirect("income")

    count = _apply_income_plan_post(request, "year", year=year)

    # Один запрос на весь месячный план года вместо SELECT на каждое из
    # ~тысячи отправленных полей формы — раньше именно это, а не сама
    # запись изменений, было причиной зависания при сохранении годового
    # плана (см. _load_expense_monthly_map).
    monthly_plan_map = _load_expense_monthly_map(year)

    dir_sums = {}
    changed = []
    for key, raw_value in request.POST.items():
        if not key.startswith(PLANMONTH_PREFIX):
            continue
        _prefix, group, category, subcategory, direction, month_str = key.split(PLAN_FIELD_SEP)
        month_num = int(month_str)
        value = _parse_money(raw_value)
        current = monthly_plan_map.get((group, category, subcategory, direction, month_num), 0.0)
        if value != current:
            changed.append((group, category, subcategory, direction, month_num, value))
        if direction:
            row_key = (group, category, subcategory, month_num)
            dir_sums[row_key] = dir_sums.get(row_key, 0.0) + value

    for group, category, subcategory, direction, month_num, value in changed:
        expense_plan.apply_monthly_plan(group, category, subcategory, direction, year, month_num, value)

    for (group, category, subcategory, month_num), total in dir_sums.items():
        current = monthly_plan_map.get((group, category, subcategory, "", month_num), 0.0)
        if total and total != current:
            expense_plan.apply_monthly_plan(group, category, subcategory, "", year, month_num, total)

    messages.success(request, f"План сохранён ({count} значений)")
    return redirect(request.POST.get("next") or "plan_year", year=year)


@login_required
def plan_save_month(request, year, month):
    """Недельный план расходов — правится прямо в месячном виде (колонки —
    недели). Трогаем только реально изменившиеся недели, и если у недели
    уже есть свои дневные данные, при изменении разносим новую сумму по
    дням заново (подтверждение уже спросили на клиенте, см. plan_grid.html
    и finance.expense_plan.redistribute_week_to_days). Направления
    суммируются в категорию целиком автоматически — тот же принцип, что и в
    недельном виде (см. plan_save). Заодно сохраняет план поступлений
    (встроенный блок "Планирование поступлений")."""
    if request.method != "POST":
        return redirect("income")

    _apply_income_plan_post(request, "month", year=year, month=month)

    # Тот же приём, что и в plan_save_year — весь недельный план одним
    # запросом вместо SELECT на каждое поле формы, см. _load_plan_map.
    plan_map = _load_plan_map()

    dir_sums = {}
    changed = []
    for key, raw_value in request.POST.items():
        if not key.startswith(PLANWEEK_PREFIX):
            continue
        _prefix, group, category, subcategory, direction, week_start_str = key.split(PLAN_FIELD_SEP)
        week_start_date = date.fromisoformat(week_start_str)
        value = _parse_money(raw_value)
        current = plan_map.get((group, category, subcategory, direction, week_start_date), 0.0)
        if value != current:
            changed.append((group, category, subcategory, direction, week_start_date, value))
        if direction:
            row_key = (group, category, subcategory, week_start_date)
            dir_sums[row_key] = dir_sums.get(row_key, 0.0) + value

    def _months_touched(week_start_date):
        week_end_date = week_start_date + timedelta(days=6)
        return {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}

    touched = set()
    for group, category, subcategory, direction, week_start_date, value in changed:
        expense_plan.apply_weekly_plan(group, category, subcategory, direction, week_start_date, value)
        expense_plan.redistribute_week_to_days(group, category, subcategory, direction, week_start_date, value)
        for y, m in _months_touched(week_start_date):
            touched.add((group, category, subcategory, direction, y, m))

    for (group, category, subcategory, week_start_date), total in dir_sums.items():
        current = plan_map.get((group, category, subcategory, "", week_start_date), 0.0)
        if total and total != current:
            expense_plan.apply_weekly_plan(group, category, subcategory, "", week_start_date, total)
            expense_plan.redistribute_week_to_days(group, category, subcategory, "", week_start_date, total)
            for y, m in _months_touched(week_start_date):
                touched.add((group, category, subcategory, "", y, m))

    # Месячную сумму (год выше) держим суммой недель этого месяца — см.
    # expense_plan.sync_monthly_from_weeks. Граничная неделя затрагивает оба
    # месяца (см. _months_touched выше).
    for group, category, subcategory, direction, y, m in touched:
        expense_plan.sync_monthly_from_weeks(group, category, subcategory, direction, y, m)

    messages.success(request, "План сохранён")
    return redirect(request.POST.get("next") or "plan_month", year=year, month=month)


@login_required
def plan_save(request, week_start):
    """Дневной план расходов — правится по дням (ExpenseDailyPlan). Без
    тумблера: сумма дней ВСЕГДА поднимается в недельный план при сохранении
    (см. PLAN.md, "новая логика планирования расходов" — в отличие от
    поступлений, где правка дня по умолчанию неделю не трогает). Заодно
    сохраняет план поступлений (встроенный блок "Планирование поступлений")."""
    if request.method != "POST":
        return redirect("income")
    week_start_date = date.fromisoformat(week_start)

    _apply_income_plan_post(request, "week", week_start_date=week_start_date)

    day_sums = {}
    for key, raw_value in request.POST.items():
        if not key.startswith(PLANDAY_PREFIX):
            continue
        _prefix, group, category, subcategory, direction, day_str = key.split(PLAN_FIELD_SEP)
        value = _parse_money(raw_value)
        expense_plan.apply_daily_plan(group, category, subcategory, direction, date.fromisoformat(day_str), value)
        row_key = (group, category, subcategory, direction)
        day_sums[row_key] = day_sums.get(row_key, 0.0) + value

    touched_categories = set()
    for (group, category, subcategory, direction), total in day_sums.items():
        expense_plan.apply_weekly_plan(group, category, subcategory, direction, week_start_date, total)
        if direction:
            touched_categories.add((group, category, subcategory))

    # если у направления обновился недельный план — сумма направлений
    # автоматически поднимается в категорию целиком (пересчёт из базы, а не
    # только из этого запроса — так учитываются и направления, которые
    # сейчас не трогали, но у которых уже есть свой недельный план)
    for group, category, subcategory in touched_categories:
        total = PlanEntry.objects.filter(
            group=group, category=category, subcategory=subcategory or "", week_start=week_start_date,
        ).exclude(direction="").aggregate(total=Sum("amount"))["total"] or 0
        if total:
            expense_plan.apply_weekly_plan(group, category, subcategory, "", week_start_date, float(total))

    # Месячную сумму (год выше) тоже держим суммой недель этого месяца — та
    # же синхронизация, что и в plan_save_field/PLANDAY.
    week_end_date = week_start_date + timedelta(days=6)
    months_touched = {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}
    synced = set()
    for group, category, subcategory, direction in day_sums:
        for y, m in months_touched:
            expense_plan.sync_monthly_from_weeks(group, category, subcategory, direction, y, m)
            synced.add((group, category, subcategory, y, m))
    for group, category, subcategory in touched_categories:
        for y, m in months_touched:
            if (group, category, subcategory, y, m) not in synced:
                expense_plan.sync_monthly_from_weeks(group, category, subcategory, "", y, m)

    messages.success(request, "План сохранён")
    return redirect(request.POST.get("next") or "income")


@login_required
def plan_save_field(request):
    """Мгновенное сохранение ОДНОЙ ячейки плана расходов при уходе с поля
    (blur) — без перезагрузки страницы и без прогона чекбоксов "без
    выходных"/плана поступлений (у них своя, более тяжёлая форма сохранения
    через общую кнопку — трогать их тут не нужно и опасно: если бы
    _save_weekend_flags отработала на этом узком POST, она бы решила, что
    все чекбоксы сняты, и тихо сбросила реальные флаги). Кладём то же
    правило "разносим только если значение реально изменилось" и тот же
    каскад, что и в plan_save_year/plan_save_month/plan_save — просто на
    одно поле; подтверждение перезаписи нижнего уровня уже спросили на
    клиенте до этого запроса."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method"}, status=405)

    field = request.POST.get("field", "")
    value = _parse_money(request.POST.get("value", ""))

    if field.startswith(PLANMONTH_PREFIX):
        try:
            _prefix, group, category, subcategory, direction, month_str = field.split(PLAN_FIELD_SEP)
            year = int(request.POST.get("year", 0))
            month_num = int(month_str)
        except (ValueError, IndexError):
            return JsonResponse({"ok": False, "error": "bad field"}, status=400)
        current = expense_plan.get_monthly_plan(group, category, subcategory, direction, year, month_num)
        payload = {"ok": True}
        if value != current:
            expense_plan.apply_monthly_plan(group, category, subcategory, direction, year, month_num, value)
            if direction:
                expense_plan.recompute_category_monthly(group, category, subcategory, year, month_num)
        if direction:
            # клиент сам считает сумму направлений по DOM (она уже отражает
            # только что сохранённое значение) и решает — блокировать
            # ячейку категории или нет; stored_value/has_lower нужны ТОЛЬКО
            # чтобы корректно разблокировать её обратно, если сумма ушла в 0
            payload["category_cell"] = {
                "field_name": expense_monthly_field_name(group, category, subcategory, None, month_num),
                "stored_value": expense_plan.get_monthly_plan(group, category, subcategory, "", year, month_num),
                "has_lower": expense_plan.month_has_weekly_data(group, category, subcategory, "", year, month_num),
                "cascade_label": "неделям месяца",
            }
        return JsonResponse(payload)

    if field.startswith(PLANWEEK_PREFIX):
        try:
            _prefix, group, category, subcategory, direction, week_start_str = field.split(PLAN_FIELD_SEP)
            week_start_date = date.fromisoformat(week_start_str)
        except (ValueError, IndexError):
            return JsonResponse({"ok": False, "error": "bad field"}, status=400)
        current = expense_plan.get_weekly_plan(group, category, subcategory, direction, week_start_date)
        payload = {"ok": True}
        if value != current:
            expense_plan.apply_weekly_plan(group, category, subcategory, direction, week_start_date, value)
            expense_plan.redistribute_week_to_days(group, category, subcategory, direction, week_start_date, value)
            # Месячную сумму (год выше) держим суммой недель этого месяца —
            # см. expense_plan.sync_monthly_from_weeks, по просьбе
            # пользователя расходы синхронизируются так же, как поступления.
            # Граничная неделя (заходит в соседний месяц) затрагивает ОБА —
            # тот же набор месяцев, что и _week_amount_from_months при
            # спуске сверху вниз.
            week_end_date = week_start_date + timedelta(days=6)
            months_touched = {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}
            for y, m in months_touched:
                expense_plan.sync_monthly_from_weeks(group, category, subcategory, direction, y, m)
            if direction:
                expense_plan.recompute_category_weekly(group, category, subcategory, week_start_date)
                for y, m in months_touched:
                    expense_plan.sync_monthly_from_weeks(group, category, subcategory, "", y, m)
        if direction:
            payload["category_cell"] = {
                "field_name": expense_week_field_name(group, category, subcategory, None, week_start_date),
                "stored_value": expense_plan.get_weekly_plan(group, category, subcategory, "", week_start_date),
                "has_lower": expense_plan.week_has_daily_data(group, category, subcategory, "", week_start_date),
                "cascade_label": "дням недели",
            }
        return JsonResponse(payload)

    if field.startswith(PLANDAY_PREFIX):
        try:
            _prefix, group, category, subcategory, direction, day_str = field.split(PLAN_FIELD_SEP)
            day = date.fromisoformat(day_str)
        except (ValueError, IndexError):
            return JsonResponse({"ok": False, "error": "bad field"}, status=400)
        week_start_date = day - timedelta(days=day.weekday())

        # Если у недели ещё нет ни одного явно сохранённого дня — сначала
        # "материализуем" текущие показанные по умолчанию значения
        # остальных дней (неделя/7), иначе первая же правка ОДНОГО дня
        # обнулила бы остальные: пересчёт недельной суммы ниже видит только
        # явно сохранённые дни, а плейсхолдер "неделя/7" нигде до этого не
        # хранился — пользователь визуально видел его как часть плана, но
        # по факту его как будто и не было.
        has_any_day = any(
            expense_plan.get_daily_plan(group, category, subcategory, direction, week_start_date + timedelta(days=i)) is not None
            for i in range(7)
        )
        if not has_any_day:
            default_v = (expense_plan.get_weekly_plan(group, category, subcategory, direction, week_start_date) or 0.0) / 7.0
            for i in range(7):
                d = week_start_date + timedelta(days=i)
                if d != day:
                    expense_plan.apply_daily_plan(group, category, subcategory, direction, d, default_v)

        expense_plan.apply_daily_plan(group, category, subcategory, direction, day, value)
        week_total = 0.0
        for i in range(7):
            v = expense_plan.get_daily_plan(group, category, subcategory, direction, week_start_date + timedelta(days=i))
            week_total += v or 0.0
        expense_plan.apply_weekly_plan(group, category, subcategory, direction, week_start_date, week_total)
        # Месячную сумму (год выше) тоже держим суммой недель этого месяца
        # — та же синхронизация, что и при прямой правке недели (см.
        # PLANWEEK-ветку выше), просто теперь неделя обновилась не прямой
        # правкой, а поднятием из дня.
        week_end_date = week_start_date + timedelta(days=6)
        months_touched = {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}
        for y, m in months_touched:
            expense_plan.sync_monthly_from_weeks(group, category, subcategory, direction, y, m)
        payload = {"ok": True}
        if direction:
            expense_plan.recompute_category_weekly(group, category, subcategory, week_start_date)
            for y, m in months_touched:
                expense_plan.sync_monthly_from_weeks(group, category, subcategory, "", y, m)
            cat_stored = expense_plan.get_daily_plan(group, category, subcategory, "", day)
            if cat_stored is None:
                cat_week_total = expense_plan.get_weekly_plan(group, category, subcategory, "", week_start_date)
                cat_stored = (cat_week_total or 0.0) / 7.0
            payload["category_cell"] = {
                "field_name": expense_daily_field_name(group, category, subcategory, None, day),
                "stored_value": cat_stored,
                "has_lower": None,
                "cascade_label": None,
            }
        return JsonResponse(payload)

    return JsonResponse({"ok": False, "error": "unknown field"}, status=400)


def _parse_money(text):
    cleaned = text.strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# ------------------------------------------------------ планирование поступлений
#
# Зеркалит "Поступления", но значения — план, а не факт. План хранится по
# неделям (IncomePlanEntry) и по месяцам (IncomeMonthlyPlan, только для
# годового ввода — см. finance.income_plan). Разбивка недели на дни зависит
# от отметки "без выходных" (IncomeWeekendFlag): 7 дней, если категория
# приносит выручку и по выходным, иначе 5 (только будни).

IPLAN_SEP = "␟"
IPLAN_PREFIX = f"iplan{IPLAN_SEP}"


# Только "Продажи от покупателей" (и её направления) продаёт регулярно,
# день за днём — остальные категории (кредиты, продажа основных средств,
# спецпроекты и т.п.) по определению не делятся на дневной план, поэтому
# дневной вид ("неделя" — 7 колонок-дней) для них не показывает ни
# редактируемых ячеек, ни отметки "без выходных" (она там не имеет смысла —
# делить нечего).
DAILY_SPLIT_CATEGORY = "Продажи от покупателей"


def income_plan_field_name(category, direction, key):
    return IPLAN_SEP.join(["iplan", category, _dir_key(direction), str(key)])


def weekend_field_name(category, direction):
    return IPLAN_SEP.join(["weekend", category, _dir_key(direction)])


@login_required
def income_plan_save_field(request):
    """Мгновенное сохранение ОДНОГО поля плана поступлений — значения
    месяца/недели/дня или чекбокса "без выходных" — тот же принцип, что и
    plan_save_field для расходов (см. её докстринг), чтобы для встроенного
    блока "Планирование поступлений" на странице "План" тоже можно было
    убрать общую кнопку "Сохранить план".

    Правка дня ВСЕГДА поднимается в недельный план (без тумблера) — та же
    логика, что и у расходов (см. PLAN.md, "новая логика планирования
    расходов" и expense_plan/plan_save_field/PLANDAY): день — самый нижний
    уровень, конфликтовать не с чем, значение недели просто пересчитывается
    как сумма её дней (нетронутые дни — 0, см. income_plan.load_daily_plan_map).
    А вот правка недели (в месячном виде) НЕ поднимается в месячный план
    автоматически — так же, как и у расходов ExpenseMonthlyPlan не
    пересчитывается из недель: план месяца — самостоятельная цифра,
    вводится отдельно (в годовом виде), недели её только детализируют.

    kind ("year"/"month"/"week") передаёт клиент — он и так знает, на какой
    странице находится; без него "iplan␟категория␟направление␟суффикс" был
    бы неоднозначен (суффикс — то месяц 1-12, то дата недели/дня)."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method"}, status=405)

    field = request.POST.get("field", "")
    kind = request.POST.get("kind", "")
    parts = field.split(IPLAN_SEP)

    if field.startswith(f"weekend{IPLAN_SEP}") and len(parts) == 3:
        _prefix, category, direction = parts
        income_plan.set_weekend_flag(category, direction, request.POST.get("value") == "1")
        return JsonResponse({"ok": True})

    if field.startswith(IPLAN_PREFIX) and len(parts) == 4:
        _prefix, category, direction, suffix = parts
        value = _parse_money(request.POST.get("value", ""))
        if kind == "year":
            year = int(request.POST.get("year", 0))
            income_plan.apply_monthly_plan(category, direction, year, int(suffix), value)
        elif kind == "month":
            week_start_date = date.fromisoformat(suffix)
            income_plan.apply_weekly_plan(category, direction, week_start_date, value)
            # Дневную разбивку этой недели тоже надо обновить — иначе она
            # останется от старой недельной суммы, и planned_daily_income
            # (смотрит в дневные данные, если они есть) будет считать
            # остаток по устаревшим дням, разъезжаясь с этой же таблицей
            # плана поступлений (та просто суммирует IncomePlanEntry
            # напрямую). Подтверждение перезаписи уже спросили на клиенте,
            # если внизу были свои дневные данные (см. plan-cascade-input).
            works_weekends = income_plan.load_weekend_flags().get((category, direction), False)
            income_plan.redistribute_week_to_days(category, direction, week_start_date, value, works_weekends)
            # И месячную сумму (год выше) тоже держим суммой недель этого
            # месяца — иначе правка недели в месячном виде осталась бы не
            # видна в годовом (та же логика, только уровнем выше). Граничная
            # неделя (заходит в соседний месяц) затрагивает оба — считаем
            # от самой недели, а не от того, какая страница сейчас открыта.
            week_end_date = week_start_date + timedelta(days=6)
            for y, m in {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}:
                income_plan.sync_monthly_from_weeks(category, direction, y, m)
        elif kind == "week":
            day = date.fromisoformat(suffix)
            week_start_date = day - timedelta(days=day.weekday())

            # Та же материализация плейсхолдеров, что и в plan_save_field
            # (PLANDAY) для расходов — см. её комментарий. Дефолт по дням
            # здесь не "неделя/7", а income_plan.daily_split (5 будних или
            # 7 дней — в зависимости от чекбокса "без выходных").
            has_any_day = any(
                income_plan.get_daily_plan(category, direction, week_start_date + timedelta(days=i)) is not None
                for i in range(7)
            )
            if not has_any_day:
                works_weekends = income_plan.load_weekend_flags().get((category, direction), False)
                weekly = income_plan.get_weekly_plan(category, direction, week_start_date)
                for d, default_v in income_plan.daily_split(weekly, week_start_date, works_weekends):
                    if default_v is not None and d != day:
                        income_plan.apply_daily_plan(category, direction, d, default_v)

            income_plan.apply_daily_plan(category, direction, day, value)
            week_total = 0.0
            for i in range(7):
                v = income_plan.get_daily_plan(category, direction, week_start_date + timedelta(days=i))
                week_total += v or 0.0
            income_plan.apply_weekly_plan(category, direction, week_start_date, week_total)
            # Месячную сумму (год выше) тоже держим суммой недель этого
            # месяца — та же синхронизация, что и при прямой правке недели
            # (см. kind == "month" выше), просто неделя обновилась не
            # прямой правкой, а поднятием из дня.
            week_end_date = week_start_date + timedelta(days=6)
            for y, m in {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}:
                income_plan.sync_monthly_from_weeks(category, direction, y, m)
        else:
            return JsonResponse({"ok": False, "error": "bad kind"}, status=400)
        return JsonResponse({"ok": True})

    return JsonResponse({"ok": False, "error": "unknown field"}, status=400)


def _save_weekend_flags(request, classified, income_categories):
    """Чекбоксы "без выходных" — часть общей формы плана (не отдельная
    авто-сабмитящаяся форма) — иначе клик по чекбоксу перезагружал страницу
    и стирал ещё не сохранённые значения плана в остальных полях. Ставятся
    и на категорию целиком, и отдельно на каждое направление — разные
    направления одной категории могут приносить выручку по-разному."""
    for category in income_categories:
        checked = request.POST.get(weekend_field_name(category, None)) == "on"
        income_plan.set_weekend_flag(category, _dir_key(None), checked)
        directions = aggregate.income_directions(classified, category)
        if len(directions) > 1:
            for direction in directions:
                d_checked = request.POST.get(weekend_field_name(category, direction)) == "on"
                income_plan.set_weekend_flag(category, _dir_key(direction), d_checked)


def _income_plan_rows(classified, income_categories, weekend_flags):
    """[{category, works_weekends, directions: [...], direction_weekends:
    {...}}, ...] — то же дерево, что и на "Поступления", плюс отметка "без
    выходных" на категорию целиком и отдельно на каждое направление."""
    rows = aggregate.category_rows(income_categories)
    out = []
    for category, _sub in rows:
        directions = aggregate.income_directions(classified, category)
        expandable = len(directions) > 1
        out.append({
            "category": category,
            "works_weekends": weekend_flags.get((category, _dir_key(None)), False),
            "directions": directions if expandable else [],
            "direction_weekends": (
                {d: weekend_flags.get((category, _dir_key(d)), False) for d in directions} if expandable else {}
            ),
            "daily_split": category == DAILY_SPLIT_CATEGORY,
        })
    return out


def _income_plan_table_year(classified, income_categories, weekend_flags, year, with_nav=True, monthly_plan_map=None):
    cat_rows = _income_plan_rows(classified, income_categories, weekend_flags)
    if monthly_plan_map is None:
        monthly_plan_map = income_plan.load_monthly_plan_map()

    columns = [{"label": aggregate.MONTH_NAMES[m - 1], "month": m} for m in range(1, 13)]
    if with_nav:
        for col in columns:
            col["nav_url"] = reverse("income_plan_month", args=[year, col["month"]])
    table = []
    col_totals = [0.0] * 12
    for row in cat_rows:
        category = row["category"]
        expandable = bool(row["directions"])

        dir_rows = []
        for direction in row["directions"]:
            d_cells = []
            d_total = 0.0
            for col in columns:
                v = income_plan.get_monthly_plan(category, _dir_key(direction), year, col["month"], plan_map=monthly_plan_map)
                d_cells.append({"value": v, "field_name": income_plan_field_name(category, direction, col["month"])})
                d_total += v
            dir_rows.append({
                "direction": direction, "cells": d_cells, "total": d_total,
                "works_weekends": row["direction_weekends"].get(direction, False),
                "weekend_field_name": weekend_field_name(category, direction),
                "daily_split": row["daily_split"],
            })

        cells = []
        row_total = 0.0
        for idx, col in enumerate(columns):
            if expandable:
                # план вводится по направлениям — общую цифру категории не
                # вводят, она считается как сумма направлений; если по
                # направлениям за этот месяц ещё ничего не ввели, показываем
                # старое значение (введённое раньше, до разбивки по
                # направлениям), чтобы оно не пропало из вида молча
                dir_sum = sum(dr["cells"][idx]["value"] for dr in dir_rows)
                value = dir_sum or income_plan.get_monthly_plan(category, None, year, col["month"], plan_map=monthly_plan_map)
                cells.append({"value": value, "editable": False})
            else:
                value = income_plan.get_monthly_plan(category, None, year, col["month"], plan_map=monthly_plan_map)
                cells.append({"value": value, "editable": True, "field_name": income_plan_field_name(category, None, col["month"])})
            row_total += value
            col_totals[idx] += value

        table.append({
            "category": category, "works_weekends": row["works_weekends"],
            "weekend_field_name": weekend_field_name(category, None),
            "cells": cells, "total": row_total, "directions": dir_rows, "expandable": expandable,
            "daily_split": row["daily_split"],
        })

    return {"columns": columns, "table": table, "col_totals": col_totals, "grand_total": sum(col_totals)}


@login_required
def income_plan_year(request, year):
    classified, income_categories, _rows = services.load_classified_operations()
    weekend_flags = income_plan.load_weekend_flags()
    result = _income_plan_table_year(classified, income_categories, weekend_flags, year)
    ctx = {
        "kind": "year", "year": year, **result,
        "period_title": f"{year} год", "home_url": None,
        "save_url": reverse("income_plan_save_year", args=[year]),
    }
    return render(request, "finance/income_plan_grid.html", ctx)


def _income_plan_table_month(classified, income_categories, weekend_flags, year, month, with_nav=True, weekly_plan_map=None):
    cat_rows = _income_plan_rows(classified, income_categories, weekend_flags)
    if weekly_plan_map is None:
        weekly_plan_map = income_plan.load_weekly_plan_map()

    weeks = aggregate.month_weeks(year, month)
    columns = [{"label": f"{s:%d.%m}–{e:%d.%m}", "start": s, "end": e} for s, e in weeks]
    table = []
    col_totals = [0.0] * len(columns)
    for row in cat_rows:
        category = row["category"]
        expandable = bool(row["directions"])

        dir_rows = []
        for direction in row["directions"]:
            d_cells = []
            d_total = 0.0
            for col in columns:
                v = income_plan.get_weekly_plan(category, _dir_key(direction), col["start"], plan_map=weekly_plan_map)
                cell = {"value": v, "editable": True, "field_name": income_plan_field_name(category, direction, col["start"].isoformat())}
                if row["daily_split"]:
                    cell["has_lower"] = income_plan.week_has_daily_data(category, _dir_key(direction), col["start"])
                d_cells.append(cell)
                d_total += v
            dir_rows.append({
                "direction": direction, "cells": d_cells, "total": d_total,
                "current_total": d_total, "edited_total": d_total,
                "works_weekends": row["direction_weekends"].get(direction, False),
                "weekend_field_name": weekend_field_name(category, direction),
                "daily_split": row["daily_split"],
            })

        cells = []
        row_total = 0.0
        for idx, col in enumerate(columns):
            if expandable:
                # план вводится по направлениям — общую цифру категории не
                # вводят, она считается как сумма направлений; если по
                # направлениям за эту неделю ещё ничего не ввели, показываем
                # старое значение (введённое раньше, до разбивки по
                # направлениям), чтобы оно не пропало из вида молча
                dir_sum = sum(dr["cells"][idx]["value"] for dr in dir_rows)
                value = dir_sum or income_plan.get_weekly_plan(category, None, col["start"], plan_map=weekly_plan_map)
                cells.append({"value": value, "editable": False})
            else:
                value = income_plan.get_weekly_plan(category, None, col["start"], plan_map=weekly_plan_map)
                cell = {"value": value, "editable": True, "field_name": income_plan_field_name(category, None, col["start"].isoformat())}
                if row["daily_split"]:
                    cell["has_lower"] = income_plan.week_has_daily_data(category, "", col["start"])
                cells.append(cell)
            row_total += value
            col_totals[idx] += value

        table.append({
            "category": category, "works_weekends": row["works_weekends"],
            "weekend_field_name": weekend_field_name(category, None),
            "cells": cells, "total": row_total, "directions": dir_rows, "expandable": expandable,
            "current_total": row_total, "edited_total": row_total,
            "daily_split": row["daily_split"],
        })

    if with_nav:
        for col in columns:
            col["nav_url"] = reverse("income_plan_week", args=[year, month, col["start"].isoformat()])
    return {"columns": columns, "table": table, "col_totals": col_totals, "grand_total": sum(col_totals)}


@login_required
def income_plan_month(request, year, month):
    classified, income_categories, _rows = services.load_classified_operations()
    weekend_flags = income_plan.load_weekend_flags()
    result = _income_plan_table_month(classified, income_categories, weekend_flags, year, month)
    ctx = {
        "kind": "month", "year": year, "month": month, **result,
        "period_title": f"{aggregate.MONTH_NAMES[month - 1]} {year}",
        "home_url": reverse("income_plan_year", args=[year]),
        "save_url": reverse("income_plan_save_month", args=[year, month]),
    }
    return render(request, "finance/income_plan_grid.html", ctx)


def _income_plan_table_week(classified, income_categories, weekend_flags, start):
    cat_rows = _income_plan_rows(classified, income_categories, weekend_flags)
    daily_plan_map = income_plan.load_daily_plan_map()

    days = [start + timedelta(days=i) for i in range(7)]
    columns = [{"label": d.strftime("%d.%m"), "day": d} for d in days]
    table = []
    col_totals = [0.0] * 7
    def _day_cells(category, direction, works_weekends, weekly_total):
        dir_key = _dir_key(direction)
        daily = income_plan.daily_split(weekly_total, start, works_weekends)
        # Если у этой строки в этой неделе уже есть хоть один явно заданный
        # день — остальные (без своего значения) считаются нулём, а не
        # дефолтом от daily_split(). Иначе, стоит поправить всего один день,
        # у остальных, ещё не тронутых, "из ниоткуда" менялся бы плейсхолдер
        # каждый раз, как меняется недельная сумма — тот же баг, что был у
        # расходов, см. views._expense_day_cells.
        has_daily_data = any((category, dir_key, d) in daily_plan_map for d, v in daily if v is not None)
        out = []
        for d, default_v in daily:
            if default_v is None:
                out.append({"value": None, "field_name": None, "editable": False})
                continue
            override = daily_plan_map.get((category, dir_key, d))
            if override is not None:
                v = override
            else:
                v = 0.0 if has_daily_data else default_v
            out.append({"value": v, "field_name": income_plan_field_name(category, direction, d.isoformat()), "editable": True})
        return out

    def _blank_cells():
        return [{"value": None, "field_name": None, "editable": False} for _ in range(7)]

    for row in cat_rows:
        category = row["category"]
        expandable = bool(row["directions"])
        daily_editable = row["daily_split"]

        dir_rows = []
        for direction in row["directions"]:
            d_works_weekends = row["direction_weekends"].get(direction, False)
            dir_key = _dir_key(direction)
            d_weekly_total = income_plan.get_weekly_plan(category, dir_key, start)
            if daily_editable:
                d_cells = _day_cells(category, direction, d_works_weekends, d_weekly_total)
                d_edited_total = sum(c["value"] or 0.0 for c in d_cells)
            else:
                d_cells = _blank_cells()
                d_edited_total = d_weekly_total
            dir_rows.append({
                "direction": direction, "cells": d_cells,
                "total": d_weekly_total, "current_total": d_weekly_total, "edited_total": d_edited_total,
                "works_weekends": d_works_weekends,
                "weekend_field_name": weekend_field_name(category, direction),
                "daily_split": daily_editable,
            })

        weekly_total = income_plan.get_weekly_plan(category, None, start)
        if expandable:
            # план вводится по направлениям — дни категории целиком не
            # редактируют, показываем сумму направлений за каждый день (если
            # эта категория вообще делится на дни — см. DAILY_SPLIT_CATEGORY)
            if daily_editable:
                cells = []
                for idx in range(7):
                    dir_sum = sum(dr["cells"][idx]["value"] or 0.0 for dr in dir_rows)
                    cells.append({"value": dir_sum, "field_name": None, "editable": False})
            else:
                cells = _blank_cells()
        elif daily_editable:
            cells = _day_cells(category, None, row["works_weekends"], weekly_total)
        else:
            cells = _blank_cells()
        for idx, cell in enumerate(cells):
            col_totals[idx] += cell["value"] or 0.0
        edited_total = sum(dr["edited_total"] for dr in dir_rows) if expandable else (
            sum(cell["value"] or 0.0 for cell in cells) if daily_editable else weekly_total
        )
        current_total = sum(dr["current_total"] for dr in dir_rows) if expandable else weekly_total

        table.append({
            "category": category, "works_weekends": row["works_weekends"],
            "weekend_field_name": weekend_field_name(category, None),
            "cells": cells, "total": weekly_total, "directions": dir_rows, "expandable": expandable,
            "current_total": current_total, "edited_total": edited_total,
            "daily_split": daily_editable,
        })

    return {"columns": columns, "table": table, "col_totals": col_totals, "grand_total": sum(col_totals)}


@login_required
def income_plan_week(request, year, month, week_start):
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    classified, income_categories, _rows = services.load_classified_operations()
    weekend_flags = income_plan.load_weekend_flags()
    result = _income_plan_table_week(classified, income_categories, weekend_flags, start)
    ctx = {
        "kind": "week", "year": year, "week_start": start.isoformat(), **result,
        "period_title": (
            f"{aggregate.MONTH_NAMES[start.month - 1]}, {start:%d.%m}–{end:%d.%m.%Y}"
        ),
        "home_url": reverse("income_plan_month", args=[year, month]),
        "save_url": reverse("income_plan_save_week", args=[week_start]),
    }
    return render(request, "finance/income_plan_grid.html", ctx)


def _apply_income_plan_post(request, kind, year=None, month=None, week_start_date=None):
    """Общая логика сохранения плана поступлений — переиспользуется и
    отдельной страницей "Планирование поступлений" (income_plan_save_*), и
    встроенным блоком на странице "План" (plan_save_year/plan_save_month/
    plan_save), чтобы не дублировать разбор полей.

    Правка дня ВСЕГДА поднимается в недельный план, без тумблера — та же
    логика, что и у расходов (см. income_plan_save_field, её докстринг, и
    expense_plan/plan_save_field/PLANDAY). Правка недели ТОЖЕ ВСЕГДА
    поднимается в месячный план (как сумма недель этого месяца) — по
    просьбе пользователя расходы синхронизируются так же, см.
    (income_plan/expense_plan).sync_monthly_from_weeks."""
    classified, income_categories, _rows = services.load_classified_operations()
    _save_weekend_flags(request, classified, income_categories)
    count = 0
    if kind == "year":
        for key, raw_value in request.POST.items():
            if not key.startswith(IPLAN_PREFIX):
                continue
            _prefix, category, direction, month_str = key.split(IPLAN_SEP)
            value = _parse_money(raw_value)
            # direction здесь уже итоговый ключ хранения (см. _dir_key) — ""
            # для категории целиком, EMPTY_DIRECTION_KEY для реального
            # направления "(без направления)", либо обычное направление.
            income_plan.apply_monthly_plan(category, direction, year, int(month_str), value)
            count += 1
    elif kind == "month":
        # Недели сохраняются как введены. Дневную разбивку недели тоже
        # обновляем (redistribute_week_to_days) — иначе она останется от
        # старой суммы и разъедется с plan_projection.planned_daily_income,
        # см. комментарий в income_plan_save_field. Месячную сумму (год
        # выше) — тем же принципом, сумма недель этого месяца, а не сама по
        # себе, см. sync_monthly_from_weeks.
        weekend_flags = income_plan.load_weekend_flags()
        touched = set()
        for key, raw_value in request.POST.items():
            if not key.startswith(IPLAN_PREFIX):
                continue
            _prefix, category, direction, week_start_str = key.split(IPLAN_SEP)
            value = _parse_money(raw_value)
            week_start_date = date.fromisoformat(week_start_str)
            income_plan.apply_weekly_plan(category, direction, week_start_date, value)
            works_weekends = weekend_flags.get((category, direction), False)
            income_plan.redistribute_week_to_days(category, direction, week_start_date, value, works_weekends)
            week_end_date = week_start_date + timedelta(days=6)
            for y, m in {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}:
                touched.add((category, direction, y, m))
            count += 1
        for category, direction, y, m in touched:
            income_plan.sync_monthly_from_weeks(category, direction, y, m)
    else:  # week
        # Каждый день сохраняется отдельно (IncomeDailyPlan), и сумма
        # изменившихся строк сразу поднимается в недельный план (без
        # тумблера — см. докстринг выше).
        weekly_sums = {}
        for key, raw_value in request.POST.items():
            if not key.startswith(IPLAN_PREFIX):
                continue
            _prefix, category, direction, day_str = key.split(IPLAN_SEP)
            value = _parse_money(raw_value)
            income_plan.apply_daily_plan(category, direction, date.fromisoformat(day_str), value)
            weekly_sums[(category, direction)] = weekly_sums.get((category, direction), 0.0) + value
            count += 1
        week_end_date = week_start_date + timedelta(days=6)
        months_touched = {(week_start_date.year, week_start_date.month), (week_end_date.year, week_end_date.month)}
        for (category, direction), total in weekly_sums.items():
            income_plan.apply_weekly_plan(category, direction, week_start_date, total)
            for y, m in months_touched:
                income_plan.sync_monthly_from_weeks(category, direction, y, m)
    return count


@login_required
def income_plan_save_year(request, year):
    if request.method != "POST":
        return redirect("income_plan")
    count = _apply_income_plan_post(request, "year", year=year)
    messages.success(request, f"План сохранён ({count} значений)")
    return redirect(request.POST.get("next") or "income_plan_year", year=year)


@login_required
def income_plan_save_month(request, year, month):
    if request.method != "POST":
        return redirect("income_plan")
    count = _apply_income_plan_post(request, "month", year=year, month=month)
    messages.success(request, f"План сохранён ({count} значений)")
    return redirect(request.POST.get("next") or "income_plan_month", year=year, month=month)


@login_required
def income_plan_save_week(request, week_start):
    if request.method != "POST":
        return redirect("income_plan")
    week_start_date = date.fromisoformat(week_start)
    count = _apply_income_plan_post(request, "week", week_start_date=week_start_date)
    messages.success(request, f"План сохранён ({count} значений)")
    return redirect(request.POST.get("next") or "income_plan")
