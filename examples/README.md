# Consultas e acompanhamento

Os exemplos vêm de dois momentos do desenvolvimento:

- **07/09/2026:** captura da API com 137 sessões e observações de 39 sessões na página do filme.
- **15/09/2026:** registro de um ciclo automático com 145 sessões e envio do resumo confirmado pelo Telegram.

O arquivo `resumo-telegram.txt` é um recorte reconstituído com os dados do ciclo de 15/09. O registro exportado contém contagens, datas e o resultado do envio.

Para analisar a captura de 07/09:

```powershell
.\.venv\Scripts\python.exe -m heimdall analisar --listar
```

A análise usa o instante salvo na captura. Para buscar a programação atual, use `consultar` com o perfil desejado.

Veja a [origem dos arquivos](historico/README.md). Os cenários simulados usados pelos testes ficam em `tests/fixtures/`.
