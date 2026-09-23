# Configuração e operação no Windows

Este guia cobre a consulta online, o Telegram e o Agendador de Tarefas. Execute os comandos na mesma conta Windows que vai rodar o monitor, sem privilégios de administrador.

## Preparar o ambiente e o perfil

Dentro da pasta do repositório:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m heimdall analisar
```

Confira `config/perfil.toml`: o perfil acompanha o relançamento de Vingadores em Campinas, de 24/09 a 01/10/2026. Para outra busca, altere os IDs e o período. O fim é exclusivo: `02/10/2026 00:00 -03:00` inclui todas as sessões de 01/10.

```powershell
.\.venv\Scripts\python.exe -m heimdall consultar
.\.venv\Scripts\python.exe -m heimdall verificar
.\.venv\Scripts\python.exe -m heimdall historico
```

`consultar` faz uma consulta sem banco. `verificar` consulta e registra as diferenças, sem enviar avisos. `historico` lê o banco local sem rede.

## Vincular o Telegram

Crie um bot próprio com `/newbot` no [BotFather](https://t.me/BotFather), seguindo o [tutorial oficial do Telegram](https://core.telegram.org/bots/tutorial). Use um bot dedicado ao projeto.

```powershell
.\.venv\Scripts\python.exe -m heimdall telegram configurar --janela
```

Cole o token no campo protegido da janela. O programa valida o bot e mostra um link de vinculação. Abra esse link, toque em **Iniciar** na conversa e volte ao terminal para continuar. O código desta execução identifica a conversa privada correta.

O modo `--janela` exige Tkinter. Se ele não estiver instalado, omita essa opção para inserir o token pelo terminal.

```powershell
.\.venv\Scripts\python.exe -m heimdall telegram status
.\.venv\Scripts\python.exe -m heimdall telegram testar
.\.venv\Scripts\python.exe -m heimdall verificar --avisar
```

`telegram status` mostra a configuração local. `telegram testar` envia uma mensagem de teste. `verificar --avisar` consulta a programação e envia as novidades compatíveis.

## Dados locais

Os dados ficam em `~/.heimdall-cinema-monitor`:

| Arquivo | Finalidade |
| --- | --- |
| `telegram.secret` | Token e conversa cifrados pela DPAPI do usuário Windows |
| `heimdall.sqlite3` | Histórico e fila de avisos |
| `monitor.json` | Configuração, intervalo e pausa |
| `monitor-state.json` | Últimos resultados, próxima tentativa e feedback |
| `monitor.log` | Log com rotação |

A DPAPI vincula a credencial à conta Windows. Ao mudar de conta ou computador, configure o Telegram novamente.

Um banco de demonstração, como `data/demo.sqlite3`, deve permanecer separado do banco online. Os comandos que aceitam `--banco` permitem selecionar um arquivo explicitamente.

## Agendar consultas

Execute o script em PowerShell 5.1 ou superior, com execução de scripts locais permitida. O período do perfil precisa estar no futuro.

```powershell
.\scripts\monitor.ps1 -Acao Preparar -Intervalo 30
.\scripts\monitor.ps1 -Acao Testar
.\scripts\monitor.ps1 -Acao Retomar
.\scripts\monitor.ps1 -Acao Status
```

`Preparar` instala a tarefa **desativada**. `Testar` verifica Python, banco e credencial no ambiente do Agendador; execute `verificar` antes para criar o banco. `Retomar` habilita a rotina. Se o PowerShell bloquear o arquivo `.ps1`, confira a política de execução de scripts da sua conta.

A tarefa usa a conta conectada ao Windows, caminhos absolutos e `pythonw.exe`. O computador precisa ficar ligado, com internet e sem suspensão. Ela não acorda o PC. Há um disparo a cada intervalo e outro no login; o programa respeita a próxima tentativa persistida, então fazer login não garante consulta imediata.

```powershell
.\scripts\monitor.ps1 -Acao Pausar
.\scripts\monitor.ps1 -Acao Retomar
.\scripts\monitor.ps1 -Acao Remover
```

`Remover` retira a tarefa e preserva os arquivos locais. Para mudar o intervalo, pause e prepare novamente. A preparação volta a deixar a tarefa desativada. A tarefa confirma sua identidade antes de alterar uma instalação existente.

Para consultar ou pausar apenas o controle local, sem executar `.ps1`:

```powershell
.\.venv\Scripts\python.exe -m heimdall monitor status
.\.venv\Scripts\python.exe -m heimdall monitor pausar
```

A pausa local faz os próximos disparos terminarem sem consulta. `monitor retomar` sozinho não habilita uma tarefa desativada no Windows; nesse caso, use o script de gerenciamento ou habilite a tarefa correspondente pela interface do Agendador. O nome contém `Heimdall-Sessoes-` e um identificador do diretório.

## Feedback e falhas

```powershell
.\.venv\Scripts\python.exe -m heimdall monitor feedback ativar
.\.venv\Scripts\python.exe -m heimdall monitor feedback desativar
```

O resumo por ciclo começa desativado. Desativá-lo mantém os avisos de novas sessões. Consultas manuais, como `consultar`, mostram o resultado no terminal.

Falha de coleta não significa zero sessões. O estado e o log distinguem erro, pausa e sucesso. Esperas da fonte são respeitadas; bloqueios de acesso e estruturas inesperadas exigem revisão. O programa tenta um aviso operacional após falhas consecutivas ou bloqueio e outro na recuperação. Sem internet, também não consegue enviar esses avisos.

Para conferir entregas:

```powershell
.\.venv\Scripts\python.exe -m heimdall avisos historico
```

Se houver lote `uncertain`, confira a conversa antes de resolver:

```powershell
.\.venv\Scripts\python.exe -m heimdall avisos resolver --lote ID_DO_LOTE --acao confirmar
```

Use `tentar-novamente` em vez de `confirmar` somente quando quiser autorizar outra tentativa, aceitando possível duplicidade caso a primeira mensagem tenha chegado. O reenvio ainda depende de nova consulta e da validade da sessão.
