# Registros das consultas

## Programação de 07/09/2026

`api-2026-09-07.json` vem da consulta ao filme `32174` em Campinas (`14`), feita às **22h07 de 07/09/2026**, horário de Brasília. A URL e o instante completo estão em `source` e `checkedAt`.

Foram retornadas 137 sessões entre 24 e 30/09. Dessas, 39 tinham Infinity Vision e 35 eram legendadas; nenhuma reunia ambos os critérios. A data de 01/10 ainda não aparecia na resposta.

`site-2026-09-07.json` contém os IDs, cinemas, horários e rótulos observados na página do filme naquele dia. A conferência cobre 20 sessões de 24/09 e 19 de 30/09. Os 39 registros coincidem com a API quando são considerados os rótulos com `display=true`.

| Sessão | Programação | Cinema | Características |
| --- | --- | --- | --- |
| `86524297` | 24/09, 16h00 | Cine Araújo Multiplex Parque Das Bandeiras | Infinity Vision + Dublado |
| `86538471` | 24/09, 20h30 | Kinoplex Dom Pedro | IMAX + Legendado |

## Ciclo de 15/09/2026

`ciclo-2026-09-15.json` reúne as contagens e datas do relatório de validação do monitor: 145 sessões, zero compatíveis e sete das oito datas publicadas. O envio do resumo teve confirmação do Telegram. Esse registro não contém a lista das 145 sessões.

O [recorte do resumo](../resumo-telegram.txt) foi reconstituído a partir desses campos; linhas sem informação no recorte foram omitidas. É um registro daquela execução, não uma consulta atual.

## Campos exportados

A captura da API foi reduzida aos campos usados pelo parser: datas, cinema, sala, sessão, características, estado de venda e URL. Os links mantêm apenas `sessionId`. Preços, endereços, IDs internos, imagens e descrições HTML foram descartados.

As 137 sessões normalizadas foram comparadas antes e depois da exportação. Os valores permaneceram iguais, exceto pelos parâmetros retirados dos links. Os registros do ciclo usam uma lista própria de contagens, datas e status; credenciais e dados da conversa ficam na instalação local.

## Reproduzir a análise

Na raiz do repositório:

```powershell
.\.venv\Scripts\python.exe -m heimdall analisar
.\.venv\Scripts\python.exe -m unittest tests.test_historical_capture
```

O resultado esperado é `Sessões na captura: 137 | Compatíveis: 0`. Os testes também conferem as 39 observações do site. A avaliação usa o instante da coleta para manter o resultado reproduzível.
