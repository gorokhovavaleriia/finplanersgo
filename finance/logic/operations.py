"""Чтение входного файла банковских операций (лист "Деньги-операции")."""

import re
from datetime import datetime

import openpyxl

SHEET_NAME = "Деньги-операции"


def _normalize_sheet_name(name):
    name = re.sub(r"[-–—\s]+", " ", name.strip().casefold())
    return name


def _find_sheet(wb):
    """Название листа с операциями у разных выгрузок клиента может слегка
    отличаться (пробелы/тире/регистр) — ищем терпимо, а не только по точному
    совпадению с SHEET_NAME."""
    if SHEET_NAME in wb.sheetnames:
        return wb[SHEET_NAME]
    target = _normalize_sheet_name(SHEET_NAME)
    for name in wb.sheetnames:
        if _normalize_sheet_name(name) == target:
            return wb[name]
    for name in wb.sheetnames:
        normalized = _normalize_sheet_name(name)
        if "деньги" in normalized or "операц" in normalized:
            return wb[name]
    if len(wb.sheetnames) == 1:
        return wb[wb.sheetnames[0]]
    raise ValueError(
        f'Не нашла лист "{SHEET_NAME}" в файле. Доступные листы: '
        f"{', '.join(wb.sheetnames)}. Уточните, какой из них с операциями."
    )


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()


def read_operations(path_or_file):
    """Список словарей {date, amount, statya, rod_statya, description} —
    только строки с проставленной датой (пустые/итоговые строки пропускаем)."""
    wb = openpyxl.load_workbook(path_or_file, data_only=True)
    ws = _find_sheet(wb)

    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    col = {name: i for i, name in enumerate(headers)}

    operations = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        raw_date = row[col["Дата"]]
        if raw_date is None:
            continue
        operations.append({
            "date": _parse_date(raw_date),
            "amount": float(row[col["Сумма"]] or 0.0),
            "statya": str(row[col["Статья"]] or "").strip(),
            "rod_statya": str(row[col["Род. статья"]] or "").strip(),
            "direction": str(row[col["Направление"]] or "").strip(),
            "description": str(row[col["Описание"]] or "").strip(),
        })
    return operations
