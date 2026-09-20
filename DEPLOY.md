# Развёртывание на PythonAnywhere

Код уже готов к продакшену (переменные окружения, git, requirements.txt).
Дальше — шаги, которые нужно сделать руками (аккаунт, оплата, домен — это
не то, что можно сделать за вас).

## 0. Перед стартом — обязательно

- [ ] **Сменить пароль администратора.** Текущий (`kiwi-admin-2026`) много
  раз звучал открытым текстом в переписке с Claude — считайте его
  скомпрометированным. Смените после первого входа на проде (или сразу
  локально: `python manage.py changepassword admin`).
- [ ] **Сгенерировать новый SECRET_KEY** (не тот, что в settings.py по
  умолчанию — тот только для локальной разработки):
  ```
  python3 -c "import secrets; print(secrets.token_urlsafe(50))"
  ```
  Сохраните значение — понадобится на шаге 4.

## 1. Аккаунт PythonAnywhere

1. Зарегистрируйтесь на pythonanywhere.com.
2. Тариф **Developer** ($10/мес; раньше назывался Hacker, отсюда путаница
   в названии) — нужен ради своего домена, HTTPS и заданий по расписанию
   (на Free нет ни того, ни другого, только `*.pythonanywhere.com` и 512MB
   диска).

## 2. Код на сервер

Проще всего — через GitHub (тогда будущие обновления — это `git pull`):

1. Создайте **приватный** репозиторий на GitHub (код бизнес-логики, лучше
   не публично) и запушьте то, что уже подготовлено локально:
   ```
   cd /Users/alexander/Desktop/kiwi-web
   git remote add origin <ssh-или-https-адрес-репозитория>
   git push -u origin main
   ```
2. В консоли PythonAnywhere (вкладка **Consoles** → **Bash**):
   ```
   git clone <адрес-репозитория> kiwi-web
   cd kiwi-web
   python3.9 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

Без GitHub — залейте код архивом через вкладку **Files** и распакуйте в
консоли (`unzip`), остальные шаги те же.

## 3. Данные

`db.sqlite3` и `finance/data/secret.rtf` в git не попадают (см.
`.gitignore`) — их нужно перенести отдельно, это ваши реальные данные:

1. Вкладка **Files** → зайдите в `kiwi-web/` → **Upload a file** →
   загрузите свои `db.sqlite3` и `finance/data/secret.rtf` с локальной
   машины (или сразу задайте токен переменной окружения — см. шаг 4,
   тогда файл не нужен).

## 4. Переменные окружения и WSGI

Вкладка **Web** → создать новое веб-приложение → **Manual configuration**
→ Python 3.9 (совпадает с той, на которой разрабатывали и тестировали приложение).

Откройте WSGI-файл по ссылке на этой же странице (обычно
`/var/www/<username>_pythonanywhere_com_wsgi.py`) и в начало, **до**
`from kiwiweb.wsgi import application`, добавьте:

```python
import os

os.environ["DJANGO_SECRET_KEY"] = "<значение из шага 0>"
os.environ["DJANGO_DEBUG"] = "False"
os.environ["DJANGO_ALLOWED_HOSTS"] = "<username>.pythonanywhere.com"
os.environ["DJANGO_CSRF_TRUSTED_ORIGINS"] = "https://<username>.pythonanywhere.com"
os.environ["FINTABLO_API_TOKEN"] = "<токен из finance/data/secret.rtf>"
```

(Если свой домен подключите позже — добавьте его в оба списка через
запятую, домены/origins без пробелов.)

На вкладке **Web**:
- **Source code**: `/home/<username>/kiwi-web`
- **Working directory**: `/home/<username>/kiwi-web`
- **Virtualenv**: `/home/<username>/kiwi-web/.venv`
- **Static files**: URL `/static/`, Directory `/home/<username>/kiwi-web/staticfiles`

В консоли:
```
cd ~/kiwi-web && source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
```

Нажмите **Reload** на вкладке Web.

## 5. Периодическая синхронизация с Финтабло

Вкладка **Tasks** (доступна с тарифа Developer и выше) → добавить ежедневную задачу:
```
cd /home/<username>/kiwi-web && .venv/bin/python manage.py sync_fintablo
```
Время — на ваше усмотрение (например, раз в сутки рано утром).

## 6. Свой домен (опционально)

Вкладка **Web** → **Add a new domain** — там же PythonAnywhere выдаст
DNS-записи для вашего регистратора. После подключения домена обновите
`DJANGO_ALLOWED_HOSTS`/`DJANGO_CSRF_TRUSTED_ORIGINS` в WSGI-файле (шаг 4)
и не забудьте включить чекбокс **Force HTTPS** на вкладке Web.

## 7. Финальная проверка

- [ ] Открывается `https://<домен>/`, логин работает.
- [ ] `/plan/2026/` и другие вкладки открываются без 500.
- [ ] Кнопка "Синхронизировать" на `/upload/` реально тянет данные из
  Финтабло (токен на проде подхватился).
- [ ] Задача в Tasks отработала хотя бы раз без ошибок (лог — там же, на
  вкладке Tasks).

## Обновление кода после первого деплоя

```
cd ~/kiwi-web && git pull
source .venv/bin/activate && pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```
Затем **Reload** на вкладке Web.
