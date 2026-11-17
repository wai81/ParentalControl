"""Command line: pcagent run | status | bonus | block | unblock | events | init | service."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import sys
from datetime import date
from pathlib import Path

from pc_agent.policy import evaluate
from pc_agent.rules_file import RulesFile, load_rules, save_rules
from pc_agent.runner import DEFAULT_INTERVAL, Paths, default_data_dir, run_agent
from pc_agent.storage import Store
from pc_shared.events import local_now
from pc_shared.rules import Rules


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(Path(args.data_dir) if args.data_dir else default_data_dir())


def cmd_run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    print(f"Агент работает (данные: {paths.data_dir}). Остановить: Ctrl+C")
    with contextlib.suppress(KeyboardInterrupt):
        run_agent(paths, dry_run=args.dry_run, interval=args.interval, console=True)
    return 0


def _child_problems(user: str) -> list[str]:
    if sys.platform != "win32":
        return []
    from pc_agent.hardening import check_child_account

    return check_child_account(user)


def cmd_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if paths.rules.exists() and not args.force:
        print(f"{paths.rules} уже существует. Перезаписать: --force")
        return 1
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    problems = _child_problems(args.child)
    for problem in problems:
        print(f"ВНИМАНИЕ: {problem}")
    if problems and not args.force:
        print("Исправьте это или повторите с --force.")
        return 1
    save_rules(paths.rules, Rules(child_user=args.child))
    print(f"Создан {paths.rules}. Отредактируйте лимиты и списки в этом файле.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if not paths.rules.exists():
        print(f"Правил нет. Создайте их: pcagent init --child ИМЯ (файл {paths.rules})")
        return 1
    rules = load_rules(paths.rules)
    now = local_now()
    with Store(paths.db) as store:
        state = store.load_day(now.date())
        history = store.history(8)
        apps = store.app_usage(now.date(), limit=10)
    d = evaluate(rules, state, now)
    print(f"Ребёнок:            {rules.child_user or '(не задан)'}")
    print(f"Разрешённые часы:   {d.window}")
    bonus = f" (вкл. бонус {state.bonus_minutes:+d})" if state.bonus_minutes else ""
    print(f"Лимит на сегодня:   {d.limit_minutes:.0f} мин{bonus}")
    print(f"Использовано:       {d.used_minutes:.0f} мин")
    print(f"Осталось:           {d.remaining_minutes:.0f} мин")
    print(f"Сейчас можно:       {'да' if d.allowed else 'нет: ' + (d.reason or '')}")
    if rules.blocked_apps:
        print(f"Запрещённые программы: {', '.join(rules.blocked_apps)}")
    if apps:
        print()
        print("Программы сегодня:")
        for app, minutes in apps:
            print(f"  {minutes:5.0f} мин  {app}")
    past = [(day, used) for day, used in history if day != now.date()]
    if past:
        print("\nПоследние дни:")
        for day, used in past[:7]:
            print(f"  {day:%d.%m %a}  {used:5.0f} мин")
    return 0


def cmd_bonus(args: argparse.Namespace) -> int:
    with Store(_paths(args).db) as store:
        store.add_bonus(date.today(), args.minutes)
    print(f"Бонус на сегодня изменён на {args.minutes:+d} мин.")
    return 0


def cmd_block(args: argparse.Namespace, blocked: bool) -> int:
    with Store(_paths(args).db) as store:
        store.set_blocked(date.today(), blocked)
    print("Компьютер заблокирован до конца дня." if blocked else "Блокировка на сегодня снята.")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    with Store(_paths(args).db) as store:
        events = store.recent_events(args.n)
    for e in events:
        details = ", ".join(f"{k}={v}" for k, v in e.data.items())
        print(f"{e.ts:%d.%m %H:%M:%S}  {e.kind.value:<16} {details}")
    return 0


def cmd_check_rules(args: argparse.Namespace) -> int:
    errors: list[str] = []
    rules = RulesFile(_paths(args).rules, on_error=errors.append).get()
    if errors:
        print(errors[0])
        return 1
    print(rules.model_dump_json(indent=2))
    return 0


def cmd_helper(args: argparse.Namespace) -> int:
    from pc_agent.helper import run_helper

    return run_helper(tray=not args.no_tray)


def cmd_service(args: argparse.Namespace) -> int:
    from pc_agent import service

    if args.action == "install":
        service.install(disable_old=args.disable_old)
    elif args.action == "remove":
        service.remove()
    else:
        service.control(args.action)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pcagent", description="Агент родительского контроля")
    parser.add_argument("--data-dir", help="папка с правилами и базой (по умолчанию ProgramData)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="запустить агента в консоли")
    p.add_argument("--dry-run", action="store_true",
                   help="только наблюдать: не завершать сеанс и не закрывать программы")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="период проверки, с")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("init", help="создать rules.json")
    p.add_argument("--child", required=True, help="имя учётной записи ребёнка")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("status", help="сколько времени использовано и осталось").set_defaults(
        func=cmd_status)
    sub.add_parser("check-rules", help="проверить rules.json").set_defaults(func=cmd_check_rules)

    p = sub.add_parser("bonus", help="добавить (или убавить) минуты на сегодня")
    p.add_argument("minutes", type=int)
    p.set_defaults(func=cmd_bonus)

    sub.add_parser("block", help="заблокировать до конца дня").set_defaults(
        func=lambda a: cmd_block(a, True))
    sub.add_parser("unblock", help="снять блокировку на сегодня").set_defaults(
        func=lambda a: cmd_block(a, False))

    p = sub.add_parser("events", help="журнал событий")
    p.add_argument("-n", type=int, default=50)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("helper", help="(служебная) помощник в сеансе ребёнка, запускает служба")
    p.add_argument("--no-tray", action="store_true", help="без значка в трее")
    p.set_defaults(func=cmd_helper)

    p = sub.add_parser("service", help="управление службой Windows (нужны права администратора)")
    p.add_argument("action", choices=["install", "remove", "start", "stop", "restart", "status"])
    p.add_argument("--disable-old", action="store_true",
                   help="при установке отключить старую версию на PowerShell")
    p.set_defaults(func=cmd_service)
    return parser


def _configure_output() -> None:
    """Make Cyrillic readable wherever the output goes.

    A real console needs nothing: Python writes Unicode to it directly. When the
    output is piped (e.g. `pcagent status | more` in PowerShell), the reader
    decodes it with the console code page (cp866 in a Russian Windows), so use
    that one. Without a console, use UTF-8. PYTHONIOENCODING overrides all this.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    code_page = ctypes.windll.kernel32.GetConsoleOutputCP() if sys.platform == "win32" else 0
    encoding = f"cp{code_page}" if code_page else "utf-8"
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure") or stream.isatty():
            continue
        stream.reconfigure(encoding=encoding, errors="replace")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv and getattr(sys, "frozen", False):
        from pc_agent.service import run_dispatcher

        if run_dispatcher():
            return 0
        # Started by a person (double-click), not by the Service Control Manager.
        _configure_output()
        build_parser().print_help()
        print()
        with contextlib.suppress(EOFError):
            input("Нажмите Enter, чтобы закрыть окно...")
        return 0
    _configure_output()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PermissionError as exc:
        print(f"Нет доступа: {exc.filename or exc}. Запустите команду от имени администратора.")
        return 1
    except Exception as exc:
        from pc_agent.service import ServiceError

        if isinstance(exc, ServiceError):
            print(exc)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
