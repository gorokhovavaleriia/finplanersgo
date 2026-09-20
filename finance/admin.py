from django.contrib import admin

from .models import (
    CategoryOverride, ExpenseOverride, IncomeMonthlyPlan, IncomePlanEntry,
    IncomeWeekendFlag, Operation, PlanEntry,
)


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ("date", "amount", "statya", "rod_statya", "direction", "source")
    list_filter = ("source", "statya")
    search_fields = ("statya", "rod_statya", "direction", "description")
    date_hierarchy = "date"


@admin.register(CategoryOverride)
class CategoryOverrideAdmin(admin.ModelAdmin):
    list_display = ("operation", "category", "subcategory")
    search_fields = ("category", "subcategory")


@admin.register(ExpenseOverride)
class ExpenseOverrideAdmin(admin.ModelAdmin):
    list_display = ("operation", "group", "category", "subcategory")
    list_filter = ("group",)
    search_fields = ("category", "subcategory")


@admin.register(PlanEntry)
class PlanEntryAdmin(admin.ModelAdmin):
    list_display = ("week_start", "group", "category", "subcategory", "direction", "amount")
    list_filter = ("group", "category")
    date_hierarchy = "week_start"


@admin.register(IncomeWeekendFlag)
class IncomeWeekendFlagAdmin(admin.ModelAdmin):
    list_display = ("category", "works_weekends")


@admin.register(IncomeMonthlyPlan)
class IncomeMonthlyPlanAdmin(admin.ModelAdmin):
    list_display = ("year", "month", "category", "direction", "amount")
    list_filter = ("category",)


@admin.register(IncomePlanEntry)
class IncomePlanEntryAdmin(admin.ModelAdmin):
    list_display = ("week_start", "category", "direction", "amount")
    list_filter = ("category",)
    date_hierarchy = "week_start"
