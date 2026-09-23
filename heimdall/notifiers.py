"""Adaptador de envio pelo Telegram."""

from hashlib import sha256

from heimdall.delivery import DeliveryError
from heimdall.telegram import TelegramClient, TelegramError
from heimdall.telegram_setup import load_credentials

class TelegramNotifier:
    def __init__(self):
        try:
            credentials = load_credentials()
            self.client = TelegramClient(credentials.token)
        except (TelegramError, OSError):
            raise DeliveryError("telegram_configuration") from None
        self.chat_id = credentials.chat_id
        self.target_key = sha256(f"{credentials.token.split(':')[0]}:{credentials.chat_id}".encode()).hexdigest()

    def send(self, batch: dict) -> str:
        try:
            return str(self.client.send_text(self.chat_id, batch["message"]))
        except TelegramError as exc:
            raise DeliveryError("telegram_uncertain" if exc.uncertain else "telegram_rejected",
                                uncertain=exc.uncertain, retry_after=exc.retry_after) from None


def make_notifier(channel: str):
    if channel == "telegram":
        return TelegramNotifier()
    raise DeliveryError("unknown_channel")
