# Дизайн: поиск по задаче в TUI-дашборде

Дата: 2026-05-26

## Цель

Добавить в TUI-дашборд `jwu` поле поиска: пользователь вводит ключ задачи
(например `WMDJANGOCHAT-25`), нажимает Enter — открывается карточка задачи
внутри дашборда.

## Контекст

Дашборд `JwuDashboard` (`src/jwu/cli/dashboard.py`) на Textual. Справа —
колонка `#changes-col` с панелью «Изменения» (`#changes-pane`, внутри
`VerticalScroll` со `Static#changes`) и строкой статуса `Static#status`.

Сейчас задача открывается выбором строки в таблице → `_open_detail(obj)` →
`push_screen(IssueDetailScreen(...))`. Данные тянутся по ключу через
`issue_get_fn` (в `main.py` это `_issue_detail`, вызывающий `svc.issue(key)`,
сетевой запрос, работает для любого ключа, не только закэшированного).

## Решения (подтверждены пользователем)

- На Enter открывается **карточка в TUI** (`IssueDetailScreen`), не браузер.
- Поле поиска **всегда видно** над блоком изменений. Отдельного хоткея для
  фокуса нет — фокус мышью/Tab.

## UI

В `compose()` внутри `Vertical(id="changes-col")` первым элементом (над
`#changes-pane`) добавляется `Input(id="search", placeholder="Поиск: KEY-123 → Enter")`.

CSS:
- `#search { height: 3; margin: 0 1; }`
- `#changes-pane` остаётся `height: 3fr`, `#status` — `height: 1fr`; поле
  поиска занимает свою фиксированную высоту сверху.

## Поведение

Обработчик `on_input_submitted(event)` (реагирует только на `#search`):

1. `key = event.value.strip().upper()`.
2. Если `key` пустой — игнорируем (выходим).
3. Запускаем фоновый поток `@work(thread=True, exclusive=True)`, который
   вызывает `self._issue_get_fn(key)` (как `IssueDetailScreen._refresh`).
   Если `_issue_get_fn is None` — ничего не делаем.
4. Успех → `self.app.call_from_thread(self._open_detail, issue)` (push_screen
   обязан выполняться в главном потоке).
5. Исключение (включая 404 от `svc.issue`) → `self.app.call_from_thread`
   показывает `self.notify("Задача {key} не найдена или недоступна",
   severity="error")`.
6. После сабмита очищаем поле: `event.input.value = ""`.

## Переиспользование

- Открытие карточки — существующий `self._open_detail(issue)` (ветка
  `isinstance(obj, Issue)`). Отдельную логику push не пишем.
- Фоновый сетевой вызов — паттерн `@work(thread=True)` +
  `call_from_thread`, уже применённый в `IssueDetailScreen._refresh`.

## Вне области (YAGNI)

Автодополнение, история поиска, поиск по тексту/нескольким полям, поиск PR.
Только точный переход по ключу задачи.

## Тесты

- Нормализация ввода: пробелы по краям убираются, ключ приводится к верхнему
  регистру (`  wmdjangochat-25  ` → `WMDJANGOCHAT-25`).
- Пустой/пробельный ввод не вызывает `issue_get_fn`.
- Непустой ввод вызывает `issue_get_fn` ровно с нормализованным ключом.
- Исключение из `issue_get_fn` не роняет приложение (ошибка → уведомление).

Тестируем логику нормализации/диспетчеризации через замоканный
`issue_get_fn`-коллбэк; запуск Textual-приложения целиком не требуется, если
вынести нормализацию в отдельную чистую функцию/метод.
