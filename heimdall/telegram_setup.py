"""Configuração interativa e credencial cifrada com DPAPI do usuário Windows."""

import ctypes
from ctypes import wintypes
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import warnings

from heimdall.telegram import TelegramClient, TelegramCredentials, TelegramError, validate_token

TEST_MESSAGE = (
    "HEIMDALL — TESTE DE NOTIFICAÇÃO\n\n"
    "A conexão com o Telegram está funcionando.\n"
    "Nenhuma consulta a sessões foi realizada neste teste.\n"
    "Os avisos de sessões usam esta conversa. Confira monitor status para saber se a rotina está liberada."
)


def credentials_path() -> Path:
    return Path.home() / ".heimdall-cinema-monitor" / "telegram.secret"


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise TelegramError("A configuração protegida do Telegram nesta etapa requer Windows.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    try:
        # UI_FORBIDDEN, sem LOCAL_MACHINE: proteção vinculada ao usuário atual.
        if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise TelegramError("Não foi possível proteger ou abrir a configuração. Use o mesmo usuário Windows que a criou.")
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel32.LocalFree(target.data)


def save_credentials(credentials: TelegramCredentials, *, path: Path | None = None, replace: bool = False):
    path = credentials_path() if path is None else path
    if path.exists() and not replace:
        raise TelegramError("Telegram já configurado. Para trocar o bot ou a conversa, use --substituir.")
    protected = _crypt(json.dumps({"version": 1, **asdict(credentials)}).encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="telegram-", suffix=".secret", delete=False) as out:
            temporary = Path(out.name)
            out.write(protected)
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # No Windows, rename falha caso outra configuração tenha criado o destino.
            os.rename(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_credentials(*, path: Path | None = None) -> TelegramCredentials:
    path = credentials_path() if path is None else path
    if not path.exists():
        raise TelegramError("Telegram ainda não configurado. Execute: python -m heimdall telegram configurar")
    with path.open("rb") as file:
        protected = file.read(16385)
    if not protected or len(protected) > 16384:
        raise TelegramError("Arquivo de configuração do Telegram inválido.")
    try:
        data = json.loads(_crypt(protected, decrypt=True).decode("utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise TelegramError("Versão de configuração do Telegram desconhecida.")
        return TelegramCredentials(data["token"], data["chat_id"], data["bot_username"])
    except (UnicodeError, KeyError, TypeError, json.JSONDecodeError):
        raise TelegramError("Conteúdo da configuração do Telegram inválido.") from None


def read_token() -> str:
    print("Copie somente o token do BotFather. No PowerShell clássico, cole com o botão direito do mouse.")
    masked = sys.version_info >= (3, 14)
    print("A entrada mostrará asteriscos; o token não será exibido." if masked else "A entrada ficará invisível; o token não será exibido.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        for attempt in range(3):
            try:
                options = {"echo_char": "*"} if masked else {}
                token = getpass.getpass("Token do Telegram (protegido): ", **options).strip()
            except getpass.GetPassWarning:
                raise TelegramError("Este terminal não permite entrada oculta. Abra o PowerShell do Windows e execute o comando nele.") from None
            try:
                return validate_token(token)
            except TelegramError:
                if not token:
                    reason = "Nenhum texto foi recebido. Cole o token antes de pressionar Enter."
                elif any(ord(char) < 32 or ord(char) == 127 for char in token):
                    reason = "O terminal recebeu um caractere de controle, possivelmente o atalho de colar. Tente colar com o botão direito do mouse."
                else:
                    reason = "O texto recebido não tem o formato de um token. Copie somente a sequência de números, dois-pontos e letras do BotFather, sem a mensagem completa."
                if attempt == 2:
                    raise TelegramError(reason + " Execute a configuração novamente para tentar outra vez.") from None
                print(reason + " Tente novamente abaixo.")


def read_token_window() -> str:
    """Recebe o token num campo de senha com suporte normal a Ctrl+V."""
    try:
        import tkinter as tk
        from tkinter import messagebox, simpledialog
    except ImportError:
        raise TelegramError("Este Python não possui Tkinter para abrir a janela de configuração.") from None
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        for attempt in range(3):
            value = simpledialog.askstring(
                "Heimdall — configurar Telegram",
                "Cole somente o token do BotFather neste campo (Ctrl+V).\n"
                "Ele será ocultado por asteriscos. Depois, clique em OK.\n"
                "A vinculação continuará no terminal.",
                show="*", parent=root,
            )
            if value is None:
                raise TelegramError("Configuração cancelada. Nenhum token foi salvo.")
            try:
                return validate_token(value.strip())
            except TelegramError:
                if attempt == 2:
                    raise TelegramError("Não foi recebido um token válido. Copie somente o token completo do BotFather e tente novamente.") from None
                messagebox.showerror(
                    "Token não reconhecido",
                    "Copie somente o token completo do BotFather, sem o texto ao redor, e cole no campo de senha.",
                    parent=root,
                )
    except tk.TclError:
        raise TelegramError("Não foi possível abrir a janela. Execute o comando em uma sessão normal do Windows.") from None
    finally:
        if root is not None:
            root.destroy()


def configure(*, replace: bool = False, window: bool = False):
    if credentials_path().exists() and not replace:
        raise TelegramError("Telegram já configurado. Para trocar o bot ou a conversa, use --substituir.")
    if os.name != "nt":
        raise TelegramError("A configuração protegida nesta etapa requer Windows.")
    token = read_token_window() if window else read_token()
    client = TelegramClient(token)
    username = client.bot_username()
    client.require_no_webhook()
    code = "heimdall_" + secrets.token_urlsafe(18)
    print(f"Bot confirmado: @{username}")
    print("Abra este link no Telegram e toque em Iniciar. O link vincula apenas a sua conversa privada:")
    print(f"https://t.me/{username}?start={code}")
    input("Depois de tocar em Iniciar, pressione Enter aqui para concluir: ")
    chat_id = client.pairing_chat(code)
    save_credentials(TelegramCredentials(token, chat_id, username), replace=replace)
    print(f"Telegram configurado para @{username}. Credencial protegida salva em: {credentials_path()}")
    print("Agora execute: python -m heimdall telegram testar")


def run_command(args) -> int:
    try:
        if args.telegram_command == "configurar":
            configure(replace=args.substituir, window=args.janela)
        elif args.telegram_command == "status":
            if not credentials_path().exists():
                print("Telegram ainda não configurado. Execute: python -m heimdall telegram configurar")
                return 0
            credentials = load_credentials()
            print(f"Configuração local disponível: @{credentials.bot_username}, conversa privada vinculada.")
            print("Status local; não verifica a conexão. Para enviar uma mensagem de teste, use: telegram testar")
        else:
            credentials = load_credentials()
            message_id = TelegramClient(credentials.token).send_text(credentials.chat_id, TEST_MESSAGE)
            print(f"Telegram confirmou o envio do teste por @{credentials.bot_username} (mensagem {message_id}). Confira a conversa.")
        return 0
    except TelegramError as exc:
        print(f"Falha no Telegram: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Falha no Telegram: não foi possível ler ou salvar a configuração local.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Operação interrompida. Se o teste estava em envio, confira a conversa antes de repetir.")
        return 1
