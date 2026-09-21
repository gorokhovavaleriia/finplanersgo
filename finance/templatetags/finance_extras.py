from django import template

register = template.Library()


@register.filter
def money(value):
    """5000 -> "5 000", 0/None -> "" (пусто, а не "0", чтобы пустые ячейки
    сетки не пестрили нулями)."""
    if not value:
        return ""
    return f"{value:,.0f}".replace(",", " ")


@register.filter
def money0(value):
    """Как money(), но 0 показывает как "0", а не пусто — для итоговых строк."""
    return f"{value or 0:,.0f}".replace(",", " ")


@register.filter
def percent(value):
    """Доля (0..1) -> проценты с 2 знаками, "25.00" — для FundIncomeShare.share
    в истории на странице "Настройка остатков" (хранится долей, вводится и
    показывается в процентах)."""
    return f"{float(value or 0) * 100:.2f}"


@register.filter
def rawnum(value):
    """Число как есть, точкой в качестве разделителя дробной части, без
    локализации Django (обычный {{ value }} для float рендерится с запятой
    из-за русской локали — ломает JS, который парсит value как число)."""
    return repr(float(value or 0))


@register.filter
def zip_lists(a, b):
    """Пара параллельных списков одинаковой длины -> [(a[0], b[0]), ...] —
    для строк "план/факт по столбцам" в шаблоне (используется вместе с
    цветом факт-vs-план, см. plan_grid.html)."""
    return zip(a or [], b or [])


@register.simple_tag
def zip3(a, b, c):
    """Как zip_lists, но для трёх списков сразу — используется, когда к
    паре факт/ok в шаблоне нужен ещё и столбец (со start/end периода) для
    кликабельной ссылки на операции, см. plan_grid.html."""
    return zip(a or [], b or [], c or [])
