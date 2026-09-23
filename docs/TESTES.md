# Testes

Execute a suíte na raiz do repositório:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

A suíte contém **169 testes**, executados com Python 3.14.6 no Windows. As chamadas de rede são simuladas para que os testes possam rodar sem configurar a API ou o Telegram.

## Organização

| Área | Verificações |
| --- | --- |
| Filtros e coleta | Formato, idioma, datas, venda habilitada, respostas inválidas e timeout |
| SQLite | Novidades, mudanças de compatibilidade, transações e reabertura do histórico |
| Telegram e entregas | Configuração, DPAPI, reserva de lotes, confirmação e envios incertos |
| Monitor | Intervalo, pausa, fim do período, feedback e recuperação de falhas |
| Integração entre processos | Retomada após falha de conexão e encerramento durante o envio |
| Captura histórica | 137 sessões da API e comparação com 39 observações do site |
| CLI | Captura real como entrada padrão e cenário positivo por arquivo de teste |

Os testes de integração iniciam processos Python separados e usam bancos temporários para verificar a persistência e a recuperação após o encerramento do programa.

## Dados usados

As [fixtures](../tests/fixtures/README.md) cobrem cenários específicos, incluindo sessões compatíveis, mudanças de programação e repetição de consultas. Os instantes são fixos para manter os resultados reproduzíveis.

Os [registros históricos](../examples/historico/README.md) permitem testar o parser com dados coletados da fonte. A regressão compara a captura de 137 sessões com as 39 observações feitas na página do filme.
