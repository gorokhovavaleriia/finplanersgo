"""Клиент API Финтабло — только сетевые запросы и пагинация, без бизнес-
логики сопоставления и без моделей Django (та же граница, что и
finance.logic.operations у Excel-варианта). Документация:
https://my.fintablo.ru/api/docs — базовый url https://api.fintablo.ru,
авторизация заголовком Authorization: Bearer <токен>, лимит 300 запросов/мин.

Токен — из переменной окружения FINTABLO_API_TOKEN, если задана (так на
проде, см. DEPLOY.md — секрет не должен лежать файлом в репозитории).
Локально переменной обычно нет, тогда читаем data/secret.rtf — так его
туда вставили (через TextEdit, получился RTF, а не plain text) — читаем и
то, и другое, чтобы не зависеть от того, как файл будет пересохранён в
следующий раз. Этот файл в .gitignore — в репозиторий не попадает."""

import os
from pathlib import Path

import requests
from striprtf.striprtf import rtf_to_text

BASE_URL = "https://api.fintablo.ru"
TOKEN_PATH = Path(__file__).resolve().parent / "data" / "secret.rtf"
REQUEST_TIMEOUT = 30
MAX_PAGE_SIZE = 1000


class FintabloError(RuntimeError):
    """Ошибка запроса к API Финтабло — авторизация, лимит, сетевая ошибка
    или ошибка сервиса (текст берётся из ответа, если он есть)."""


def _load_token():
    env_token = os.environ.get("FINTABLO_API_TOKEN", "").strip()
    if env_token:
        return env_token
    if not TOKEN_PATH.exists():
        raise FintabloError(
            f"Не найден токен Финтабло — ни в переменной окружения FINTABLO_API_TOKEN, ни в файле {TOKEN_PATH}"
        )
    raw = TOKEN_PATH.read_text(encoding="utf-8", errors="ignore")
    text = rtf_to_text(raw) if raw.lstrip().startswith("{\\rtf") else raw
    token = text.strip()
    if not token:
        raise FintabloError(f"Файл с токеном Финтабло пуст: {TOKEN_PATH}")
    return token


def _request(method, path, params=None, json_body=None):
    token = _load_token()
    try:
        resp = requests.request(
            method, f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params, json=json_body, timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise FintabloError(f"Финтабло: сетевая ошибка — {e}") from e

    if resp.status_code == 401:
        raise FintabloError("Финтабло: ошибка авторизации — проверьте токен в finance/data/secret.rtf")
    if resp.status_code == 429:
        raise FintabloError("Финтабло: превышен лимит запросов (300/мин) — повторите позже")

    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise FintabloError("Финтабло: сервер вернул не-JSON ответ")

    if data.get("status") != 200:
        raise FintabloError(f"Финтабло: {data.get('statusText') or 'неизвестная ошибка'}")
    return data.get("items") or []


def list_categories():
    """Все статьи ДДС компании — {id, name, parentId, group, ...}."""
    return _request("GET", "/v1/category")


def list_directions():
    """Все направления компании — {id, name, parentId, ...}."""
    return _request("GET", "/v1/direction")


def list_transactions(date_from=None, date_to=None):
    """Все операции ДДС за период (даты — строки "дд.мм.гггг", либо None =
    без ограничения с этой стороны) — сама пролистывает страницы (лимит
    Финтабло — 1000 записей на страницу)."""
    page = 1
    items = []
    while True:
        params = {"page": page, "pageSize": MAX_PAGE_SIZE}
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        batch = _request("GET", "/v1/transaction", params=params)
        items.extend(batch)
        if len(batch) < MAX_PAGE_SIZE:
            break
        page += 1
    return items
