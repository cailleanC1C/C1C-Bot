import logging


_pylog = logging.getLogger("c1c")


class _Log:
    def human(self, level: str, message: str, **fields):
        levelno = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }.get(level.lower(), logging.INFO)
        _pylog.log(levelno, message, extra=fields)


log = _Log()


def channel_label(guild, channel_id):
    if channel_id in (None, ""):
        return "#unknown"
    return f"<#{channel_id}>"


def user_label(guild, user_id):
    if user_id in (None, ""):
        return "unknown"
    return f"<@{user_id}>"
