import sys

from heimdall.cli import main

# Mantém acentos legíveis também quando o Windows redireciona a saída.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

raise SystemExit(main())
