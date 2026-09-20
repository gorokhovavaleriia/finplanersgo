"""Разнесение операций по категориям поступлений — без единого вопроса
пользователю. Всё, что не удалось уверенно определить, уходит в ту же
"Неразнесенное поступление", что и в самом листе "Поступления" — там это
настоящая категория (строка 12), а не придуманная нами.

Категория теперь определяется прямым построчным сопоставлением "статья ->
категория" из листа "Поступления" (см. categories.load_income_statya_map) —
фиксированных подкатегорий больше нет, вместо них разворот по
"направлению" на экране (см. aggregate.income_directions)."""

UNALLOCATED = "Неразнесенное поступление"

EXCLUDED_STATYAS = {"Перевод между счетами"}
_EXCLUDED_HINTS = ("взаиморасч", "взаимозачет", "взаимозачёт")


def is_excluded(statya):
    """Статьи, которые вообще не считаются ни поступлением, ни расходом
    (переводы между счетами, взаиморасчёты/взаимозачёты) — не должны
    попадать ни в категоризацию, ни в список операций на экране."""
    statya = statya.strip()
    if statya in EXCLUDED_STATYAS:
        return True
    statya_lower = statya.lower()
    return any(hint in statya_lower for hint in _EXCLUDED_HINTS)


def classify_income(op, income_statya_map):
    """op: словарь из operations.read_operations (amount > 0 уже
    проверено вызывающим кодом). Возвращает (категория, None) или None,
    если операцию вообще не нужно учитывать (переводы/взаиморасчёты)."""
    statya = op["statya"].strip()
    if is_excluded(statya):
        return None
    category = income_statya_map.get(statya.casefold())
    if not category:
        return UNALLOCATED, None
    return category, None
