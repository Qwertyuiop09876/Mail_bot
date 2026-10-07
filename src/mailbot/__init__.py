"""mailbot — a library for quality email campaigns over Yandex and custom-domain mailboxes."""

from .app import MailBot
from .config import Settings

__version__ = "0.1.0"
__all__ = ["MailBot", "Settings", "__version__"]
