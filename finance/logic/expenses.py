"""Разнесение расходов по фондам — из data/operation_map.xlsx.

Формат изменился: раньше лист "Расходы" был деревом группа -> категория ->
подкатегории (3 уровня), теперь и "Расходы", и новый лист "кредиторка" —
построчная таблица "статья/род.статья -> фонд". Категория у расхода теперь
одноуровневая (без подкатегорий) — это то же самое "название в таблице
фондов", что и раньше, просто без дерева над ним.

Лист "Сопоставление" (отдельная построчная таблица "статья/род.статья ->
отображаемое имя") — старая логика, больше не используется нигде в коде: он
отдельно хранил то же самое имя категории, что уже есть в "Расходы" (через
род.статью — см. classify_expense/expense_rows ниже), и реально разошёлся с
ним (несколько статей были там ошибочно сведены к одному "Непредвиденные
расходы" вместо своего имени). Единственный источник истины теперь —
"Расходы"/"кредиторка": имя категории — это род.статья строки, если она
задана (так фонд объединяет несколько статей под одним именем, например
"Вспомогательное сырье"/"Основное сырье" -> "Закуп сырья"), иначе сама
статья."""

from collections import OrderedDict
from pathlib import Path

import openpyxl

MAP_PATH = Path(__file__).resolve().parent.parent / "data" / "operation_map.xlsx"

# Статья из Финтабло, которой нет в листе "Расходы" (например, в Финтабло
# завели новую статью, а таблицу ещё не обновили). Такие расходы не
# приписываются ни одному настоящему фонду (это исказило бы его остаток) —
# они собираются в отдельный псевдо-фонд "Неопознанные расходы" в самом
# низу экрана, каждая статья — своей строкой (под своим настоящим именем из
# Финтабло), чтобы было видно, что именно нужно добавить в "Расходы" (см.
# services.load_classified_operations, где эти строки динамически
# дописываются в конец expense_rows()).
UNALLOCATED_GROUP = "Неопознанные расходы"

# Доля поступлений на каждый фонд (лист "Фонды") — короткое имя там, длинное
# в листах "Расходы"/"кредиторка"; сводим к длинному, оно и классифицирует
# расходы, оно и показывается на экране.
FUND_SHORT_TO_LONG = {
    "Обязательные": "Фонд обязательных платежей",
    "Переменные": "Фонд переменных платежей",
    "Непостоянные платежи": "Фонд непостоянных платежей",
    "Копилка": "Копилка (резерв)",
}

# Порядок фондов на экране (задан пользователем) — не совпадает ни с
# порядком строк в листе "Расходы", ни с порядком строк в листе "Фонды",
# поэтому и expense_rows(), и load_fund_balances() явно сортируют по этому
# списку; неизвестные фонды (если появятся) добавляются в конец.
FUND_ORDER = list(FUND_SHORT_TO_LONG.values()) + ["Фонд чистой прибыли"]


def _in_fund_order(fund_names):
    return [f for f in FUND_ORDER if f in fund_names] + [f for f in fund_names if f not in FUND_ORDER]


def _norm(text):
    return str(text).strip().casefold() if text else ""


def _read_mapping_rows(ws):
    """[(статья, род.статья_или_None, значение), ...] — пропускает
    заголовочные/пустые строки."""
    rows = []
    for row in range(1, ws.max_row + 1):
        statya = ws.cell(row=row, column=1).value
        rod_statya = ws.cell(row=row, column=2).value
        target = ws.cell(row=row, column=3).value
        if not statya or not target:
            continue
        # первые 2 строки листа — заголовки ("Названия в финтабло" / ...
        # / "Фонды" и "Статья" / "Род. статья"), а не данные
        if _norm(statya) in ("статья", "названия в финтабло"):
            continue
        rows.append((str(statya).strip(), str(rod_statya).strip() if rod_statya else None, str(target).strip()))
    return rows


def _build_lookup(rows):
    by_statya, by_rod_statya = {}, {}
    for statya, rod_statya, target in rows:
        by_statya[_norm(statya)] = target
        if rod_statya:
            by_rod_statya[_norm(rod_statya)] = target
    return {"by_statya": by_statya, "by_rod_statya": by_rod_statya}


def _raw_expense_rows(path=None):
    """[(статья, род.статья_или_None, фонд), ...] — как есть в листах
    "Расходы"+"кредиторка", без схлопывания в словарь (нужны сырые строки,
    чтобы и classify_expense, и expense_rows() одинаково решали, что взять
    именем категории — статью или род.статью, см. модульный docstring)."""
    wb = openpyxl.load_workbook(path or MAP_PATH, data_only=True)
    return _read_mapping_rows(wb["Расходы"]) + _read_mapping_rows(wb["кредиторка"])


def load_fund_map(path=None):
    """{статья/род.статья (casefold) -> фонд} — листы "Расходы" и
    "кредиторка" вместе (кредиторка — небольшое отдельное дополнение в том
    же построчном формате)."""
    return _build_lookup(_raw_expense_rows(path))


def load_fund_balances(path=None):
    """OrderedDict{фонд (длинное имя) -> {"balance": ..., "share": ...}} —
    лист "Фонды", остаток на 30.08.2026 и доля поступлений. "Фонд чистой
    прибыли" сюда не входит — у него нет автоматической доли (см. PLAN.md)."""
    wb = openpyxl.load_workbook(path or MAP_PATH, data_only=True)
    ws = wb["Фонды"]
    result = OrderedDict()
    for row in range(1, ws.max_row + 1):
        name = ws.cell(row=row, column=1).value
        balance = ws.cell(row=row, column=2).value
        share = ws.cell(row=row, column=3).value
        if not name or balance is None or share is None:
            continue
        long_name = FUND_SHORT_TO_LONG.get(str(name).strip(), str(name).strip())
        result[long_name] = {"balance": float(balance), "share": float(share)}
    return OrderedDict((f, result[f]) for f in _in_fund_order(result))


def classify_expense(op, fund_map):
    """op: словарь из operations.read_operations (amount < 0 уже проверено
    вызывающим кодом). Возвращает (фонд, имя, None) — третий элемент всегда
    None, категория теперь одноуровневая, оставлен для совместимости с
    остальным кодом, который ждёт тройку (группа, категория, подкатегория).

    Имя категории — род.статья операции, если она есть и распознана
    (объединяет несколько статей под одним именем, как в "Расходы"), иначе
    сама статья. Название всегда берём из самой операции (а не из строки
    таблицы) — оно и совпадает с ней по построению, зато сохраняет
    оригинальный регистр/пробелы, как их прислал Финтабло."""
    statya_norm = _norm(op["statya"])
    rod_norm = _norm(op["rod_statya"])

    if rod_norm and rod_norm in fund_map["by_rod_statya"]:
        return fund_map["by_rod_statya"][rod_norm], op["rod_statya"].strip(), None
    if statya_norm in fund_map["by_statya"]:
        return fund_map["by_statya"][statya_norm], op["statya"].strip(), None
    # Настоящее имя статьи из Финтабло, а не выдуманная заглушка — так
    # разные неопознанные статьи не сливаются в одну строку, и видно, что
    # именно добавить в operation_map.xlsx.
    return UNALLOCATED_GROUP, (op["statya"].strip() or op["rod_statya"].strip() or "(без статьи)"), None


def expense_rows(path=None):
    """[(фонд, имя, None), ...] — сгруппировано по фонду (все имена одного
    фонда идут подряд), строго по тому, что реально есть в листах
    "Расходы"/"кредиторка" — без придуманной строки "Нераспределенное" в
    конце (в файле её нет; неопознанные операции показываются отдельным
    псевдо-фондом UNALLOCATED_GROUP, дописываемым динамически — см.
    services.load_classified_operations).

    Имя строки — то же правило, что и в classify_expense (род.статья, если
    задана, иначе статья), поэтому предзаявленный список категорий здесь
    всегда совпадает с тем, что реально проставится операциям.

    Лист "Расходы" построчно перечисляет статьи не по фондам, а как есть в
    Финтабло — один и тот же фонд у соседних строк может не повторяться
    (статья A -> Обязательные, статья B -> Непостоянные, статья C ->
    Обязательные снова). Если строки таблицы брать в этом же порядке,
    заголовок фонда на экране будет открываться заново при каждой смене —
    поэтому здесь имена сначала собираются по фонду, и только потом
    разворачиваются в плоский список — сами фонды идут в заданном порядке
    (FUND_ORDER), а не в том, в котором они впервые встретились в листе."""
    by_fund = OrderedDict()
    for statya, rod_statya, fund in _raw_expense_rows(path):
        name = rod_statya or statya
        names = by_fund.setdefault(fund, [])
        if name not in names:
            names.append(name)

    rows = []
    for fund in _in_fund_order(by_fund):
        for name in by_fund[fund]:
            rows.append((fund, name, None))
    return rows
