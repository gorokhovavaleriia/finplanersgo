from django.db import models


class Operation(models.Model):
    """Одна строка банковской операции. source отличает, как она сюда
    попала — сейчас всегда "excel", позже добавится "fintablo_api", когда
    появится доступ к API Финтабло; остальной код от источника не зависит."""

    SOURCE_EXCEL = "excel"
    SOURCE_FINTABLO_API = "fintablo_api"
    SOURCE_CHOICES = [
        (SOURCE_EXCEL, "Загрузка Excel"),
        (SOURCE_FINTABLO_API, "API Финтабло"),
    ]

    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    statya = models.CharField("Статья", max_length=255, blank=True)
    rod_statya = models.CharField("Род. статья", max_length=255, blank=True)
    direction = models.CharField("Направление", max_length=255, blank=True)
    description = models.TextField("Описание", blank=True)

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_EXCEL)
    external_id = models.CharField(
        max_length=255, blank=True, null=True, unique=True,
        help_text="ID операции в источнике (для API Финтабло) — чтобы не задваивать при повторной синхронизации",
    )
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["date"])]

    def __str__(self):
        return f"{self.date} {self.amount} {self.statya}"


class CategoryOverride(models.Model):
    """Ручная правка категории поступления — побеждает автоматическую
    (см. finance.logic.classify)."""

    operation = models.OneToOneField(Operation, on_delete=models.CASCADE, related_name="category_override")
    category = models.CharField(max_length=255)
    subcategory = models.CharField(max_length=255, blank=True, null=True)


class ExpenseOverride(models.Model):
    """Ручная правка группы/категории/подкатегории расхода — побеждает
    автоматическую (см. finance.logic.expenses)."""

    operation = models.OneToOneField(Operation, on_delete=models.CASCADE, related_name="expense_override")
    group = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    subcategory = models.CharField(max_length=255, blank=True, null=True)


class PlanEntry(models.Model):
    """Плановая сумма расхода на одну неделю — для категории целиком
    (direction пусто) или отдельно по направлению (см. вид "План")."""

    group = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    subcategory = models.CharField(max_length=255, blank=True, default="")
    direction = models.CharField(max_length=255, blank=True, default="")
    week_start = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "category", "subcategory", "direction", "week_start"],
                name="unique_plan_entry",
            )
        ]

    def __str__(self):
        return f"{self.group}/{self.category}/{self.subcategory or '-'}/{self.direction or '-'} {self.week_start}: {self.amount}"


class ExpenseDailyPlan(models.Model):
    """Плановая сумма расхода на конкретный день — дневной вид "План". См.
    PLAN.md, "новая логика планирования расходов": правка дня при
    сохранении ВСЕГДА поднимается в недельный план (PlanEntry) — без
    тумблера, в отличие от поступлений. А вот правка недели сверху (в
    месячном виде), когда дни уже что-то содержат, наоборот спускается вниз
    только с подтверждением пользователя (см. finance.expense_plan)."""

    group = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    subcategory = models.CharField(max_length=255, blank=True, default="")
    direction = models.CharField(max_length=255, blank=True, default="")
    day = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "category", "subcategory", "direction", "day"],
                name="unique_expense_daily_plan",
            )
        ]

    def __str__(self):
        return f"{self.group}/{self.category}/{self.subcategory or '-'}/{self.direction or '-'} {self.day}: {self.amount}"


class ExpenseMonthlyPlan(models.Model):
    """Плановая сумма расхода на весь месяц целиком (вводится в годовом
    виде) — источник истины для авто-разбивки по неделям, см.
    finance.expense_plan.apply_monthly_plan (зеркалит IncomeMonthlyPlan)."""

    group = models.CharField(max_length=255)
    category = models.CharField(max_length=255)
    subcategory = models.CharField(max_length=255, blank=True, default="")
    direction = models.CharField(max_length=255, blank=True, default="")
    year = models.IntegerField()
    month = models.IntegerField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "category", "subcategory", "direction", "year", "month"],
                name="unique_expense_monthly_plan",
            )
        ]

    def __str__(self):
        return f"{self.group}/{self.category}/{self.subcategory or '-'}/{self.direction or '-'} {self.year}-{self.month}: {self.amount}"


class IncomeWeekendFlag(models.Model):
    """Отметка "без выходных" — приносит выручку и по выходным (тогда
    недельный план делится на 7 дней), или нет (тогда план только на будни,
    делится на 5). Ставится либо на категорию целиком (direction пусто),
    либо отдельно на направление — разные направления одной категории могут
    работать по-разному."""

    category = models.CharField(max_length=255)
    direction = models.CharField(max_length=255, blank=True, default="")
    works_weekends = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["category", "direction"], name="unique_income_weekend_flag"),
        ]

    def __str__(self):
        return f"{self.category}/{self.direction or '-'}: {'без выходных' if self.works_weekends else 'будни'}"


class IncomeMonthlyPlan(models.Model):
    """Плановая сумма поступления на весь месяц целиком (вводится в
    годовом виде) — источник истины для авто-разбивки по неделям, см.
    finance.income_plan.apply_monthly_plan."""

    category = models.CharField(max_length=255)
    direction = models.CharField(max_length=255, blank=True, default="")
    year = models.IntegerField()
    month = models.IntegerField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["category", "direction", "year", "month"], name="unique_income_monthly_plan",
            )
        ]

    def __str__(self):
        return f"{self.category}/{self.direction or '-'} {self.year}-{self.month}: {self.amount}"


class IncomePlanEntry(models.Model):
    """Плановая сумма поступления на одну неделю — для категории целиком
    (direction пусто) или отдельно по направлению. Пересчитывается заново
    при сохранении месячного плана (см. IncomeMonthlyPlan), либо правится
    вручную напрямую в месячном/недельном виде — тогда правка живёт, пока
    относящийся месячный план не пересохранят снова."""

    category = models.CharField(max_length=255)
    direction = models.CharField(max_length=255, blank=True, default="")
    week_start = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["category", "direction", "week_start"], name="unique_income_plan_entry",
            )
        ]

    def __str__(self):
        return f"{self.category}/{self.direction or '-'} {self.week_start}: {self.amount}"


class IncomeDailyPlan(models.Model):
    """Плановая сумма поступления на конкретный день — правится в дневном
    виде (см. PLAN.md, тумблер "текущий план"/"отредактированный").
    Независима от недельного плана: правка дня не пересчитывает неделю, если
    пользователь явно не попросил (тумблер в положении "отредактированный")
    — иначе не получилось бы просто поправить/посмотреть один день, не
    трогая план недели."""

    category = models.CharField(max_length=255)
    direction = models.CharField(max_length=255, blank=True, default="")
    day = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["category", "direction", "day"], name="unique_income_daily_plan"),
        ]

    def __str__(self):
        return f"{self.category}/{self.direction or '-'} {self.day}: {self.amount}"


class FundTransfer(models.Model):
    """Ручное перемещение денег между фондами (не через доход/расход — сам
    факт распределения между "кошельками") — см. finance.fund_balance,
    учитывается в остатке дня перемещения так же, как поступления/расходы:
    у фонда-источника вычитается, у фонда-назначения прибавляется."""

    date = models.DateField()
    from_fund = models.CharField("Откуда", max_length=255)
    to_fund = models.CharField("Куда", max_length=255)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} {self.from_fund} → {self.to_fund}: {self.amount}"
