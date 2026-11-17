class PcServerError(Exception):
    """Base error. Subclasses override the class attributes; no __init__ boilerplate."""

    status_code: int = 500
    detail: str = "Непредвиденная ошибка."

    def __init__(self, detail: str | None = None, status_code: int | None = None, **context):
        self.detail = detail if detail is not None else self.__class__.detail
        self.status_code = status_code if status_code is not None else self.__class__.status_code
        self.context = context
        super().__init__(self.detail)


class NotFoundError(PcServerError):
    status_code = 404
    detail = "Не найдено."


class AuthError(PcServerError):
    status_code = 401
    detail = "Нужна авторизация."


class InvalidPairingCode(PcServerError):
    status_code = 400
    detail = "Код подключения неверный или устарел. Создайте новый в веб-панели."
