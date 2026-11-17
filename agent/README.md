# pc-agent — агент родительского контроля (Python)

Новая версия агента на Python. Она постепенно заменит скрипты PowerShell в корне репозитория.
**Не запускайте обе версии одновременно**: каждая будет считать время и завершать сеанс сама по себе.

## Что умеет сейчас

- Учитывает время, только когда сеанс ребёнка на экране и не заблокирован.
  Время между проверками считается по монотонным часам, поэтому перевод часов на ПК
  не добавляет и не убирает минуты. Сон или гибернация тоже не засчитываются.
- Задаёт дневной лимит (отдельно для будней и выходных) и разрешённые часы,
  принимает бонусное время и блокировку до конца дня.
- Показывает предупреждения (по умолчанию за 10, 5 и 1 мин). Когда время вышло,
  через минуту сеанс завершается (`logoff`) или возвращается на экран входа (`lock`).
- Закрывает запрещённые программы, имена можно задавать с `*`.
- Ведёт журнал в SQLite: запуск и завершение программ, предупреждения,
  завершения сеанса, ошибки.
- Сообщения ребёнку показывает прямо из службы (`WTSSendMessage`),
  отдельная задача от имени ребёнка не нужна.

## Разработка

```bash
uv sync
uv run pytest
uv run ruff check .
```

Пробный запуск без последствий: агент ничего не закрывает и не завершает сеанс, данные хранятся в отдельной папке.

```bash
uv run pcagent --data-dir .\devdata init --child ИмяРебёнка
uv run pcagent --data-dir .\devdata run --dry-run
uv run pcagent --data-dir .\devdata status
uv run pcagent --data-dir .\devdata events -n 30
```

## Сборка и установка службы

Сборка создаёт папку `dist\pcagent\` с `pcagent.exe`. Python на компьютере ребёнка не нужен.

```bash
powershell -ExecutionPolicy Bypass -File scripts\build-agent.ps1
```

Установка (в терминале, запущенном от имени администратора):

```bash
dist\pcagent\pcagent.exe service install
```

Что делает `service install`:
- копирует программу в `C:\Program Files\ParentalControl\agent\`, куда ребёнок не может ничего записать;
- закрывает папку данных `C:\ProgramData\ParentalControl\agent\` от записи для обычных пользователей;
- регистрирует службу `ParentalControlAgent`: запуск от имени SYSTEM, автоматический старт
  с Windows и перезапуск через 5 секунд, если процесс упал или его завершили;
- запускает службу.

Повторный `service install` из новой сборки обновляет уже установленную службу.
Остальные команды управления: `service status | start | stop | restart | remove`.

После установки создайте правила, тоже от имени администратора:

```bash
"C:\Program Files\ParentalControl\agent\pcagent.exe" init --child ИмяРебёнка
```

Проверка службы целиком: установка, перезапуск после завершения процесса, обновление и удаление.
Скрипт после себя всё удаляет.

```bash
powershell -ExecutionPolicy Bypass -File scripts\test-service.ps1
```

## Команды

| Команда | Что делает |
|---|---|
| `pcagent init --child NAME` | создаёт `rules.json` |
| `pcagent check-rules` | проверяет `rules.json` |
| `pcagent run [--dry-run]` | запускает агент в консоли |
| `pcagent status` | показывает использованное и оставшееся время, историю |
| `pcagent bonus 30` / `bonus -15` | меняет время на сегодня |
| `pcagent block` / `unblock` | блокирует до конца дня или снимает блокировку |
| `pcagent events -n 50` | выводит журнал |
| `pcagent service install/status/start/stop/restart/remove` | управляет службой Windows (только из собранного exe) |

По умолчанию данные хранятся в `C:\ProgramData\ParentalControl\agent\`: `rules.json`, `agent.db`, `agent.log`.
Другую папку можно задать через `--data-dir` или переменную окружения `PC_AGENT_DATA`.

## Пример rules.json

```json
{
  "child_user": "Kid",
  "weekdays": { "limit_minutes": 120, "window": { "start": "08:00", "end": "21:00" } },
  "weekends": { "limit_minutes": 180, "window": { "start": "09:00", "end": "22:00" } },
  "warn_at_minutes": [10, 5, 1],
  "enforcement": "logoff",
  "blocked_apps": ["RobloxPlayerBeta", "Minecraft*"],
  "blocked_sites": []
}
```

Если в изменённом `rules.json` ошибка, агент продолжит работать по прежним правилам
и запишет в журнал событие `rules_error`.

## Пока не сделано

- Подпись exe цифровым сертификатом. Без неё SmartScreen или антивирус могут предупреждать о `pcagent.exe`.
- Helper в сеансе ребёнка: активное окно и время в каждой программе, учёт бездействия, значок в трее.
- Блокировка сайтов (`blocked_sites` пока не применяется), расширение браузера.
- Синхронизация с сервером.
