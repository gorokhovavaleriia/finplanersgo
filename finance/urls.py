from django.urls import path

from . import views

urlpatterns = [
    path("", views.income_default, name="income"),
    path("expenses/", views.expenses_default, name="expenses"),
    path("plan/", views.plan_default, name="plan"),
    path("upload/", views.upload_operations, name="upload"),
    path("sync-fintablo/", views.sync_fintablo_now, name="sync_fintablo"),
    path("sync-fintablo-full/", views.sync_fintablo_full, name="sync_fintablo_full"),
    path("fund-transfers/", views.fund_transfers, name="fund_transfers"),
    path("fund-transfers/<int:pk>/delete/", views.fund_transfer_delete, name="fund_transfer_delete"),

    # Поступления
    path("income/<int:year>/", views.income_year, name="income_year"),
    path("income/<int:year>/<int:month>/", views.income_month, name="income_month"),
    path("income/<int:year>/<int:month>/<str:week_start>/", views.income_week, name="income_week"),
    path("income/ops/", views.income_ops, name="income_ops"),

    # Расходы
    path("expenses/<int:year>/", views.expenses_year, name="expenses_year"),
    path("expenses/<int:year>/<int:month>/", views.expenses_month, name="expenses_month"),
    path("expenses/<int:year>/<int:month>/<str:week_start>/", views.expenses_week, name="expenses_week"),
    path("expenses/ops/", views.expenses_ops, name="expenses_ops"),

    # План (расходы)
    path("plan/<int:year>/", views.plan_year, name="plan_year"),
    path("plan/<int:year>/<int:month>/", views.plan_month, name="plan_month"),
    path("plan/<int:year>/<int:month>/<str:week_start>/", views.plan_week, name="plan_week"),
    path("plan/save-year/<int:year>/", views.plan_save_year, name="plan_save_year"),
    path("plan/save-month/<int:year>/<int:month>/", views.plan_save_month, name="plan_save_month"),
    path("plan/save/<str:week_start>/", views.plan_save, name="plan_save"),
    path("plan/save-field/", views.plan_save_field, name="plan_save_field"),
    path("income-plan/save-field/", views.income_plan_save_field, name="income_plan_save_field"),

    # Планирование поступлений
    path("income-plan/", views.income_plan_default, name="income_plan"),
    path("income-plan/<int:year>/", views.income_plan_year, name="income_plan_year"),
    path("income-plan/<int:year>/<int:month>/", views.income_plan_month, name="income_plan_month"),
    path("income-plan/<int:year>/<int:month>/<str:week_start>/", views.income_plan_week, name="income_plan_week"),
    path("income-plan/save-year/<int:year>/", views.income_plan_save_year, name="income_plan_save_year"),
    path("income-plan/save-month/<int:year>/<int:month>/", views.income_plan_save_month, name="income_plan_save_month"),
    path("income-plan/save-week/<str:week_start>/", views.income_plan_save_week, name="income_plan_save_week"),
]
