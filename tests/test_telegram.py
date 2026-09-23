"""Bot API e armazenamento de credenciais com DPAPI."""

from contextlib import redirect_stderr, redirect_stdout
import getpass
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from heimdall.cli import main
from heimdall.telegram import TelegramClient, TelegramCredentials, TelegramError, _NoRedirect
from heimdall.telegram_setup import TEST_MESSAGE, _crypt, configure, load_credentials, read_token, read_token_window, save_credentials

TOKEN = "123456789:" + "ficticio_para_testes_" * 2
CHAT = 12345
USERNAME = "heimdall_test_bot"


class Response(BytesIO):
    def __init__(self, data, url, *, status=200):
        super().__init__(json.dumps(data).encode("utf-8"))
        self.url, self.status = url, status

    def geturl(self):
        return self.url


def update(chat=CHAT, *, kind="private", code="codigo", sender=None):
    return {"message": {"text": f"/start {code}", "chat": {"type": kind, "id": chat},
                        "from": {"is_bot": False, "id": chat if sender is None else sender}}}


class TelegramTests(unittest.TestCase):
    def client(self, result=None, *, payload=None, status=200):
        opener = Mock()
        body = {"ok": True, "result": result} if payload is None else payload
        opener.open.side_effect = lambda request, **kwargs: Response(body, request.full_url, status=status)
        return TelegramClient(TOKEN, opener=opener), opener

    def test_send_is_one_post_plain_text_to_private_chat(self):
        client, opener = self.client({"message_id": 9, "chat": {"id": CHAT, "type": "private"}})
        self.assertEqual(client.send_text(CHAT, "<teste> & sessão"), 9)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"chat_id": CHAT, "text": "<teste> & sessão",
                         "link_preview_options": {"is_disabled": True}})
        self.assertNotIn(TOKEN, request.data.decode())
        opener.open.assert_called_once()

    def test_token_input_and_repr_do_not_reveal_secret(self):
        self.assertNotIn(TOKEN, repr(TelegramCredentials(TOKEN, CHAT, USERNAME)))
        for token in ["", "https://example.com/", TOKEN + "/sendMessage", "token com espaços"]:
            with self.subTest(token=token), self.assertRaises(TelegramError):
                TelegramClient(token)

    def test_ambiguous_send_response_requires_confirmation_before_retry(self):
        cases = [
            (200, {}),
            (200, {"ok": None}),
            (200, {"ok": False}),
            (200, {"ok": False, "error_code": 200}),
            (500, {"ok": False, "error_code": 400}),
            (400, {"ok": True, "error_code": 400}),
            (200, {"ok": False, "error_code": 499}),
        ]
        for status, payload in cases:
            with self.subTest(status=status, payload=payload):
                client, opener = self.client(payload=payload, status=status)
                with self.assertRaises(TelegramError) as caught:
                    client.send_text(CHAT, "teste")
                self.assertTrue(caught.exception.uncertain)
                opener.open.assert_called_once()

    def test_invalid_timeout_chat_and_message_never_access_network(self):
        for timeout in [0, -1, 61, float("inf"), float("nan")]:
            with self.assertRaises(TelegramError):
                TelegramClient(TOKEN, timeout=timeout)
        client, opener = self.client()
        for chat in [-1000, 0, True, "12345", 2**52]:
            with self.assertRaises(TelegramError):
                client.send_text(chat, "teste")
        for message in ["", "x" * 4097, "😀" * 2049]:
            with self.assertRaises(TelegramError):
                client.send_text(CHAT, message)
        opener.open.assert_not_called()

    def test_http_errors_are_sanitized_and_never_retried(self):
        for code in [400, 401, 403, 409, 429, 500]:
            client, opener = self.client()
            body = BytesIO(json.dumps({"ok": False, "error_code": code, "description": TOKEN,
                                      "parameters": {"retry_after": 23}}).encode())
            opener.open.side_effect = HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", code, TOKEN, {}, body)
            with self.subTest(code=code), self.assertRaises(TelegramError) as caught:
                client.send_text(CHAT, "teste")
            self.assertNotIn(TOKEN, str(caught.exception))
            if code == 429:
                self.assertIn("23 segundos", str(caught.exception))
            opener.open.assert_called_once()
            self.assertTrue(body.closed)

    def test_network_error_traceback_hides_original_url(self):
        client, opener = self.client()
        opener.open.side_effect = URLError(f"falhou https://api.telegram.org/bot{TOKEN}/sendMessage")
        try:
            client.send_text(CHAT, "teste")
        except TelegramError:
            output = traceback.format_exc()
        else:
            self.fail("Falha de rede não foi detectada")
        self.assertNotIn(TOKEN, output)
        self.assertIn("resultado é incerto", output)
        opener.open.assert_called_once()

    def test_invalid_or_oversized_response_fails(self):
        for payload in [[], {"ok": True}, {"ok": False, "error_code": []}]:
            client, _ = self.client(payload=payload)
            with self.assertRaises(TelegramError):
                client.bot_username()
        for body in [b"<html>erro</html>", b"x" * (1024 * 1024 + 1)]:
            client, opener = self.client()
            response = Response({}, f"https://api.telegram.org/bot{TOKEN}/getMe")
            response.read = Mock(return_value=body)
            opener.open.side_effect = None
            opener.open.return_value = response
            with self.assertRaises(TelegramError):
                client.bot_username()

    def test_redirect_and_foreign_response_rejected(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))
        client, opener = self.client()
        opener.open.side_effect = lambda request, **kw: Response({}, "https://example.com")
        with self.assertRaises(TelegramError):
            client.bot_username()

    def test_bot_identity_and_webhook_validation(self):
        client, _ = self.client({"id": 123456789, "is_bot": True, "username": USERNAME})
        self.assertEqual(client.bot_username(), USERNAME)
        for result in [{"id": 99, "is_bot": True}, {"id": 123456789, "is_bot": False}]:
            client, _ = self.client(result)
            with self.assertRaises(TelegramError):
                client.bot_username()
        client, _ = self.client({"url": ""})
        client.require_no_webhook()
        client, _ = self.client({"url": "https://example.com/" + TOKEN})
        with self.assertRaises(TelegramError) as caught:
            client.require_no_webhook()
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_pairing_requires_exact_code_private_chat_and_same_sender(self):
        client, opener = self.client([None, {}, update(kind="group"), update(code="antigo"),
                                     update(sender=99), update(), update()])
        self.assertEqual(client.pairing_chat("codigo"), CHAT)
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("offset", payload)
        self.assertEqual(payload["timeout"], 0)
        for results in [[], [update(code="antigo")], [update(), update(chat=99)]]:
            client, _ = self.client(results)
            with self.assertRaises(TelegramError):
                client.pairing_chat("codigo")

    def test_send_confirmation_must_match_private_destination(self):
        for result in [{}, {"message_id": True, "chat": {"id": CHAT, "type": "private"}},
                       {"message_id": 9, "chat": {"id": 99, "type": "private"}},
                       {"message_id": 9, "chat": {"id": CHAT, "type": "group"}}]:
            client, _ = self.client(result)
            with self.assertRaises(TelegramError):
                client.send_text(CHAT, "teste")

    @unittest.skipUnless(os.name == "nt", "DPAPI exige Windows")
    def test_real_dpapi_roundtrip_and_reopen_without_plaintext_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.secret"
            credentials = TelegramCredentials(TOKEN, CHAT, USERNAME)
            save_credentials(credentials, path=path)
            self.assertNotIn(TOKEN.encode(), path.read_bytes())
            self.assertEqual(load_credentials(path=path), credentials)
            with self.assertRaises(TelegramError):
                _crypt(b"arquivo corrompido", decrypt=True)

    def test_save_failure_preserves_previous_configuration_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.secret"
            path.write_bytes(b"anterior")
            credentials = TelegramCredentials(TOKEN, CHAT, USERNAME)
            with self.assertRaises(TelegramError):
                save_credentials(credentials, path=path)
            with patch("heimdall.telegram_setup._crypt", return_value=b"cifrado"), patch("heimdall.telegram_setup.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    save_credentials(credentials, path=path, replace=True)
            self.assertEqual(path.read_bytes(), b"anterior")
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_missing_oversized_and_malformed_credentials_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.secret"
            with self.assertRaises(TelegramError):
                load_credentials(path=path)
            path.write_bytes(b"x" * 16385)
            with self.assertRaises(TelegramError):
                load_credentials(path=path)
            path.write_bytes(b"cifrado")
            for plaintext in [b"not json", b"{}", b'{"version":1}', b'{"version":2}']:
                with patch("heimdall.telegram_setup._crypt", return_value=plaintext), self.assertRaises(TelegramError):
                    load_credentials(path=path)

    def test_wizard_only_saves_after_pairing_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as folder, patch("heimdall.telegram_setup.credentials_path", return_value=Path(folder) / "telegram.secret"), patch("heimdall.telegram_setup.getpass.getpass", return_value=TOKEN), patch("heimdall.telegram_setup.TelegramClient") as factory, patch("heimdall.telegram_setup.save_credentials") as save, patch("builtins.input"), redirect_stdout(StringIO()) as output:
            client = factory.return_value
            client.bot_username.return_value = USERNAME
            client.pairing_chat.return_value = CHAT
            configure()
            save.assert_called_once_with(TelegramCredentials(TOKEN, CHAT, USERNAME), replace=False)
            client.send_text.assert_not_called()
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn(f"https://t.me/{USERNAME}?start=heimdall_", output.getvalue())
            client.pairing_chat.side_effect = TelegramError("não vinculado")
            save.reset_mock()
            with self.assertRaises(TelegramError):
                configure()
            save.assert_not_called()

    def test_wizard_refuses_visible_token_input(self):
        with tempfile.TemporaryDirectory() as folder, patch("heimdall.telegram_setup.credentials_path", return_value=Path(folder) / "telegram.secret"), patch("heimdall.telegram_setup.getpass.getpass", side_effect=getpass.GetPassWarning), patch("heimdall.telegram_setup.TelegramClient") as client, redirect_stdout(StringIO()):
            with self.assertRaises(TelegramError):
                configure()
            client.assert_not_called()

    def test_token_entry_masks_on_python_314_and_keeps_older_python_supported(self):
        for version, options in [((3, 14), {"echo_char": "*"}), ((3, 13), {})]:
            with self.subTest(version=version), patch("heimdall.telegram_setup.sys.version_info", version), patch("heimdall.telegram_setup.getpass.getpass", return_value=TOKEN) as entry, redirect_stdout(StringIO()) as output:
                self.assertEqual(read_token(), TOKEN)
                entry.assert_called_once_with("Token do Telegram (protegido): ", **options)
                self.assertNotIn(TOKEN, output.getvalue())

    def test_empty_input_and_raw_paste_shortcut_retry_without_exposing_input(self):
        with patch("heimdall.telegram_setup.getpass.getpass", side_effect=["", "\x16", TOKEN]) as entry, redirect_stdout(StringIO()) as output:
            self.assertEqual(read_token(), TOKEN)
            self.assertEqual(entry.call_count, 3)
            self.assertIn("Nenhum texto foi recebido", output.getvalue())
            self.assertIn("caractere de controle", output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertNotIn("\x16", output.getvalue())

    def test_invalid_pasted_text_stops_after_three_attempts_without_network(self):
        with tempfile.TemporaryDirectory() as folder, patch("heimdall.telegram_setup.credentials_path", return_value=Path(folder) / "telegram.secret"), patch("heimdall.telegram_setup.getpass.getpass", return_value="token: " + TOKEN) as entry, patch("heimdall.telegram_setup.TelegramClient") as client, redirect_stdout(StringIO()) as output:
            with self.assertRaises(TelegramError) as caught:
                configure()
            self.assertEqual(entry.call_count, 3)
            client.assert_not_called()
            self.assertNotIn(TOKEN, output.getvalue() + str(caught.exception))

    def test_token_entry_accepts_whitespace_around_complete_token(self):
        with patch("heimdall.telegram_setup.getpass.getpass", return_value="  " + TOKEN + "  "), redirect_stdout(StringIO()):
            self.assertEqual(read_token(), TOKEN)

    def test_window_entry_masks_token_and_destroys_window(self):
        with patch("tkinter.Tk") as root, patch("tkinter.simpledialog.askstring", return_value=" " + TOKEN + " ") as dialog, redirect_stdout(StringIO()) as output:
            self.assertEqual(read_token_window(), TOKEN)
            self.assertEqual(dialog.call_args.kwargs["show"], "*")
            self.assertEqual(dialog.call_args.kwargs["parent"], root.return_value)
            root.return_value.withdraw.assert_called_once()
            root.return_value.destroy.assert_called_once()
            self.assertNotIn(TOKEN, output.getvalue())

    def test_window_cancel_does_not_access_network_or_save(self):
        with tempfile.TemporaryDirectory() as folder, patch("heimdall.telegram_setup.credentials_path", return_value=Path(folder) / "telegram.secret"), patch("tkinter.Tk") as root, patch("tkinter.simpledialog.askstring", return_value=None), patch("heimdall.telegram_setup.TelegramClient") as client, patch("heimdall.telegram_setup.save_credentials") as save:
            with self.assertRaisesRegex(TelegramError, "cancelada"):
                configure(window=True)
            client.assert_not_called()
            save.assert_not_called()
            root.return_value.destroy.assert_called_once()

    def test_window_invalid_input_can_retry_without_showing_secret(self):
        with patch("tkinter.Tk") as root, patch("tkinter.simpledialog.askstring", side_effect=["", "prefixo " + TOKEN, TOKEN]), patch("tkinter.messagebox.showerror") as error:
            self.assertEqual(read_token_window(), TOKEN)
            self.assertEqual(error.call_count, 2)
            self.assertNotIn(TOKEN, str(error.call_args_list))
            root.return_value.destroy.assert_called_once()

    def test_window_creation_failure_is_sanitized(self):
        import tkinter
        with patch("tkinter.Tk", side_effect=tkinter.TclError(TOKEN)):
            with self.assertRaises(TelegramError) as caught:
                read_token_window()
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_cli_window_setup_uses_dialog_and_never_console_token_input(self):
        with tempfile.TemporaryDirectory() as folder, patch("heimdall.telegram_setup.credentials_path", return_value=Path(folder) / "telegram.secret"), patch("heimdall.telegram_setup.read_token_window", return_value=TOKEN) as dialog, patch("heimdall.telegram_setup.read_token") as console, patch("heimdall.telegram_setup.TelegramClient") as factory, patch("heimdall.telegram_setup.save_credentials") as save, patch("builtins.input"), redirect_stdout(StringIO()) as output:
            factory.return_value.bot_username.return_value = USERNAME
            factory.return_value.pairing_chat.return_value = CHAT
            self.assertEqual(main(["telegram", "configurar", "--janela"]), 0)
            dialog.assert_called_once()
            console.assert_not_called()
            save.assert_called_once_with(TelegramCredentials(TOKEN, CHAT, USERNAME), replace=False)
            self.assertNotIn(TOKEN, output.getvalue())

    def test_cli_status_without_config_is_offline_and_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as folder, patch("heimdall.telegram_setup.credentials_path", return_value=Path(folder) / "telegram.secret"), patch("heimdall.telegram_setup.TelegramClient") as client, redirect_stdout(StringIO()):
            self.assertEqual(main(["telegram", "status"]), 0)
            self.assertEqual(list(Path(folder).iterdir()), [])
            client.assert_not_called()

    def test_cli_test_sends_only_explicit_test_to_configured_destination(self):
        credentials = TelegramCredentials(TOKEN, CHAT, USERNAME)
        with patch("heimdall.telegram_setup.load_credentials", return_value=credentials), patch("heimdall.telegram_setup.TelegramClient") as factory, redirect_stdout(StringIO()) as output:
            factory.return_value.send_text.return_value = 7
            self.assertEqual(main(["telegram", "testar"]), 0)
            factory.return_value.send_text.assert_called_once_with(CHAT, TEST_MESSAGE)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn("TESTE", TEST_MESSAGE)

    def test_cli_os_error_is_sanitized(self):
        with patch("heimdall.telegram_setup.load_credentials", side_effect=OSError(TOKEN)), redirect_stderr(StringIO()) as output:
            self.assertEqual(main(["telegram", "testar"]), 1)
            self.assertNotIn(TOKEN, output.getvalue())


if __name__ == "__main__":
    unittest.main()
