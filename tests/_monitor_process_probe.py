"""Processo auxiliar com transporte HTTP simulado."""

from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.error import URLError

from heimdall.monitor import execute
from heimdall.notifications import verify_and_notify
from heimdall.telegram import TelegramCredentials
from tests.test_rules import synthetic_payload

BASE = datetime.fromisoformat("2026-09-15T12:00:00+00:00")


class Response(BytesIO):
    def __init__(self, url, payload):
        body = json.dumps(payload).encode("utf-8")
        super().__init__(body)
        self.url = url
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(body))

    def geturl(self):
        return self.url


def main():
    directory = Path(sys.argv[1])
    mode = sys.argv[2]
    now = BASE + timedelta(minutes=int(sys.argv[3]))

    def record(kind, **fields):
        with (directory / "simulated-network.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False) + "\n")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or timezone.utc)

    class SourceOpener:
        def open(self, request, *, timeout):
            assert request.method == "GET"
            record("source")
            if mode == "source_error":
                raise URLError("simulated offline")
            return Response(request.full_url, synthetic_payload())

    class TelegramOpener:
        def open(self, request, *, timeout):
            assert request.method == "POST" and request.full_url.endswith("/sendMessage")
            payload = json.loads(request.data)
            message = payload["text"]
            kind = "feedback" if "RESUMO DA CONSULTA" in message else (
                "session" if "Novas sessões compatíveis" in message else "health")
            record(kind, text=message)
            if mode == "crash_send" and kind == "session":
                # A mensagem foi recebida pelo remetente simulado; a confirmação não voltou.
                os._exit(87)
            return Response(request.full_url, {"ok": True, "result": {
                "message_id": 1, "chat": {"id": payload["chat_id"], "type": "private"}}})

    credentials = TelegramCredentials("12345:" + "F" * 35, 123456, "fixture_bot")
    with patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden")), \
         patch("heimdall.sources.http.build_opener", return_value=SourceOpener()), \
         patch("heimdall.sources.http.datetime", FrozenDateTime), \
         patch("heimdall.telegram.build_opener", return_value=TelegramOpener()), \
         patch("heimdall.notifiers.load_credentials", return_value=credentials):
        return execute(directory, clock=lambda: now,
                       verifier=lambda profile, database: verify_and_notify(profile, database, clock=lambda: now))


if __name__ == "__main__":
    raise SystemExit(main())
