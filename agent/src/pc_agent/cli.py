"""Command line: pcagent run | status | bonus | block | unblock | events | init | service."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import getpass
import os
import sqlite3
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


def _read_password(args: argparse.Namespace, prompt: str = "Пароль родителя: ") -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    return getpass.getpass(prompt)


def _via_service(args: argparse.Namespace, message: dict[str, object]) -> int:
    """Run a parent command through the service with the parent's password.

    Used when this account cannot write the agent's data (e.g. from the child's
    account), or with --via-service.
    """
    from pc_agent.pipe_client import send_command

    reply = send_command({**message, "password": _read_password(args)})
    print(reply.get("message", reply))
    return 0 if reply.get("ok") else 1


def _write_directly(args: argparse.Namespace, change) -> bool:  # noqa: ANN001
    """Apply a change to the database; False if this account may not write it."""
    if args.via_service:
        return False
    try:
        with Store(_paths(args).db) as store:
            change(store)
        return True
    except (PermissionError, sqlite3.OperationalError):
        return False


def cmd_bonus(args: argparse.Namespace) -> int:
    if _write_directly(args, lambda store: store.add_bonus(date.today(), args.minutes)):
        print(f"Время на сегодня изменено на {args.minutes:+d} мин.")
        return 0
    return _via_service(args, {"action": "bonus", "minutes": args.minutes})


def cmd_block(args: argparse.Namespace, blocked: bool) -> int:
    if _write_directly(args, lambda store: store.set_blocked(date.today(), blocked)):
        print("Компьютер заблокирован до конца дня." if blocked else "Блокировка на сегодня снята.")
        return 0
    return _via_service(args, {"action": "block" if blocked else "unblock"})


def cmd_set_password(args: argparse.Namespace) -> int:
    from pc_agent.secret import MIN_LENGTH, save_password

    paths = _paths(args)
    admin = sys.platform == "win32" and bool(ctypes.windll.shell32.IsUserAnAdmin())
    if args.data_dir is None and not admin:
        print("Нужны права администратора: запустите терминал от имени администратора.")
        return 1
    first = _read_password(args, f"Новый пароль родителя (не короче {MIN_LENGTH} символов): ")
    second = first if args.password_stdin else getpass.getpass("Повторите пароль: ")
    if first != second:
        print("Пароли не совпадают.")
        return 1
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        save_password(paths.data_dir, first, restrict=admin)
    except ValueError as exc:
        print(f"Пароль не сохранён: {exc}.")
        return 1
    if not admin:
        print("Внимание: без прав администратора файл с паролем не защищён от чтения.")
    print("Пароль родителя сохранён. Теперь управлять можно и из учётной записи ребёнка: "
          "значок в трее > Родитель.")
    return 0


def _is_admin() -> bool:
    return sys.platform == "win32" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def cmd_connect(args: argparse.Namespace) -> int:
    from pc_agent.sync import ServerError, connect

    paths = _paths(args)
    admin = _is_admin()
    if args.data_dir is None and not admin:
        print("Нужны права администратора: запустите терминал от имени администратора.")
        return 1
    name = args.name or os.environ.get("COMPUTERNAME", "Компьютер")
    try:
        reply = connect(paths.data_dir, args.server, args.code, name, restrict=admin)
    except ServerError as exc:
        print(f"Не удалось подключиться: {exc}")
        return 1
    print(f"Компьютер подключён к серверу {args.server} (устройство {reply.device_id}, "
          f"'{name}').")
    print("Служба начнёт обмен данными в течение минуты. Правила теперь меняются в веб-панели.")
    if args.server.lower().startswith("http://"):
        print("Внимание: соединение без шифрования (http). Это допустимо только в домашней сети "
              "или через VPN (например, Tailscale). Через интернет используйте https.")
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    from pc_agent.sync import disconnect

    if disconnect(_paths(args).data_dir):
        print("Компьютер отключён от сервера. Контроль продолжает работать по последним правилам.")
    else:
        print("Компьютер не был подключён к серверу.")
    return 0


def cmd_parent(args: argparse.Namespace) -> int:
    from pc_agent.dialog import run_dialog

    return run_dialog()


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

    def parent_command(name: str, help_text: str) -> argparse.ArgumentParser:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--via-service", action="store_true",
                         help="через службу с паролем родителя "
                              "(так работает без прав администратора)")
        cmd.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
        return cmd

    p = parent_command("bonus", "добавить (или убавить) минуты на сегодня")
    p.add_argument("minutes", type=int)
    p.set_defaults(func=cmd_bonus)
    parent_command("block", "заблокировать до конца дня").set_defaults(
        func=lambda a: cmd_block(a, True))
    parent_command("unblock", "снять блокировку на сегодня").set_defaults(
        func=lambda a: cmd_block(a, False))

    p = sub.add_parser("set-password", help="задать пароль родителя (от имени администратора)")
    p.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_set_password)
    sub.add_parser("parent", help="окно родителя: добавить время, заблокировать").set_defaults(
        func=cmd_parent)

    p = sub.add_parser("connect", help="подключить к серверу родителя (от имени администратора)")
    p.add_argument("--server", required=True, help="адрес сервера, например http://192.168.1.10:8000")
    p.add_argument("--code", required=True, help="код подключения из веб-панели")
    p.add_argument("--name", help="название компьютера в веб-панели")
    p.set_defaults(func=cmd_connect)
    sub.add_parser("disconnect", help="отключить от сервера").set_defaults(func=cmd_disconnect)

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
