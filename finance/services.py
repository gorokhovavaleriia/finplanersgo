"""Склейка моделей Django с проверенной бизнес-логикой из finance.logic —
она работает со списком простых словарей, а не с QuerySet, поэтому здесь
только преобразование туда и обратно."""

from .logic import aggregate, categories, expenses
from .models import Operation


def _op_to_dict(op):
    return {
        "date": op.date,
        "amount": float(op.amount),
        "statya": op.statya,
        "rod_statya": op.rod_statya,
        "direction": op.direction,
        "description": op.description,
    }


def load_classified_operations():
    """Список классифицированных операций (как в finance.logic.aggregate),
    плюс поле "id" — id операции в базе, чтобы страницы могли сослаться на
    неё при ручной правке категории. Ручные правки (CategoryOverride /
    ExpenseOverride) уже учтены.

    Возвращает (classified, income_categories, expense_rows) — третий
    элемент теперь готовый список строк [(фонд, имя, None), ...], а не
    дерево (подкатегорий у расходов больше нет, см. finance.logic.expenses)."""
    qs = list(
        Operation.objects.all()
        .select_related("category_override", "expense_override")
        .order_by("date", "id")
    )

    income_categories = categories.load_income_categories()
    income_statya_map = categories.load_income_statya_map()
    expense_fund_map = expenses.load_fund_map()

    overrides, expense_overrides = {}, {}
    for i, op in enumerate(qs):
        if hasattr(op, "category_override"):
            overrides[i] = (op.category_override.category, op.category_override.subcategory or None)
        if hasattr(op, "expense_override"):
            expense_overrides[i] = (
                op.expense_override.group, op.expense_override.category, op.expense_override.subcategory or None,
            )

    classified = aggregate.classify_operations(
        [_op_to_dict(op) for op in qs],
        income_statya_map,
        overrides,
        expense_fund_map=expense_fund_map,
        expense_overrides=expense_overrides,
    )
    for op_row, op in zip(classified, qs):
        op_row["id"] = op.pk

    expense_rows = expenses.expense_rows()

    # Статьи, которых нет в "Расходы" — про них заранее ничего не известно
    # (в отличие от остальных expense_rows, взятых из operation_map.xlsx),
    # поэтому находим их только теперь, по факту классификации, и
    # дописываем в конец списка строк — см.
    # expenses.UNALLOCATED_GROUP/classify_expense.
    unidentified = sorted({
        op["exp_category"] for op in classified if op["exp_group"] == expenses.UNALLOCATED_GROUP
    })
    if unidentified:
        expense_rows = expense_rows + [(expenses.UNALLOCATED_GROUP, category, None) for category in unidentified]

    return classified, income_categories, expense_rows
