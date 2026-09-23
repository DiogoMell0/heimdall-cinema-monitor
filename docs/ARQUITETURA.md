# Decisões técnicas

A entrada da aplicação é `python -m heimdall`. O pacote separa coleta, filtros, persistência e envio para que cada etapa possa ser testada isoladamente.

## Uma consulta, várias verificações

`sources/http.py` consulta a programação por filme e cidade. Faz um GET com timeout e limite de resposta, sem repetir a requisição dentro do mesmo ciclo nem seguir redirecionamentos. `sources/ingresso.py` transforma o JSON nos objetos definidos em `models.py`.

O perfil é um arquivo TOML. `rules.py` verifica as características da mesma sessão, o intervalo, o início futuro e os campos de venda. `service.py` aplica as regras e identifica as datas procuradas que ainda não aparecem na programação.

A consulta usa HTTP/JSON porque a resposta da API já continha os campos necessários. Um navegador automatizado acrescentaria consumo de recursos e dependência da interface. Mudanças no formato da fonte podem exigir revisão do coletor.

## SQLite e identificação de novidades

SQLite atende a uma aplicação local sem exigir um servidor de banco separado. `storage.py` mantém consultas (`runs`), sessões (`sessions`), eventos (`events`) e entregas (`deliveries` e `delivery_batches`).

O ID da sessão e a identidade do perfil permitem comparar o que já era conhecido. Uma sessão nova compatível gera evento; uma sessão conhecida que passa de incompatível para compatível também. Repetir a mesma programação não cria outro evento. A ausência temporária de uma sessão não é tratada como mudança de idioma ou formato.

As alterações são gravadas em transações. A captura antiga não substitui uma mais recente. Bancos de reprodução offline e de consulta online são separados para que a demonstração não contamine o acompanhamento real.

## Entregar uma mensagem também tem estado

`notifications.py` coordena consulta, histórico e fila. `delivery.py` reavalia a validade da sessão antes do envio, agrupa novidades e reserva cada lote no SQLite antes de acessar o Telegram. A chamada de rede fica fora da transação.

Após confirmação, o lote fica `sent`. Uma recusa explícita mantém a possibilidade de uma tentativa posterior, respeitando a espera. Uma queda de conexão pode acontecer depois que o Telegram aceitou a mensagem: nesse caso, o resultado é `uncertain`, sem reenvio automático. A recuperação de um processo interrompido segue essa mesma regra.

`telegram.py` trata o protocolo da Bot API. `telegram_setup.py` vincula a conversa e cifra a credencial com DPAPI.

## Um processo curto por disparo

`scripts/monitor.ps1` registra a tarefa do Windows. Cada disparo inicia `pythonw.exe`, que executa um ciclo e termina. Não é necessário manter um terminal aberto. Um bloqueio entre processos e a configuração da tarefa evitam sobreposição.

`monitor.py` guarda a próxima tentativa permitida, o resultado e as falhas em arquivos locais. A espera aumenta após falhas transitórias; uma instrução maior de espera da fonte prevalece. Recusa de acesso ou dados inesperados interrompem consultas até revisão. Quando termina o período do perfil, o monitor deixa de consultar.

O feedback de `monitor_feedback.py` usa a captura e as diferenças já calculadas. Não faz uma segunda consulta. Há uma tentativa de resumo por ciclo, sem acumular mensagens antigas. Seu estado é separado da fila durável de novidades de sessões.

## Dependências e dados locais

- Biblioteca padrão do Python: `urllib`, `sqlite3`, `tomllib`, `unittest` e módulos de apoio.
- Histórico, credencial e estado do monitor em `~/.heimdall-cinema-monitor`.
- Capturas históricas em `examples/historico/` e cenários de teste em `tests/fixtures/`.
- O comando `analisar` usa a captura de 07/09/2026 por padrão, com o instante de avaliação salvo no arquivo.

A comparação da captura da API com as 39 observações do site faz parte da suíte de regressão.
