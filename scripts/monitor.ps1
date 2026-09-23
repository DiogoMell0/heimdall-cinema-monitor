#requires -Version 5.1
# Instalação e controle da tarefa no Agendador do Windows.
[CmdletBinding()]
param(
    [ValidateSet('Preparar', 'Status', 'Pausar', 'Retomar', 'Executar', 'Testar', 'Remover')]
    [string]$Acao = 'Status',
    [string]$Diretorio = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.heimdall-cinema-monitor'),
    [ValidateRange(1, 1440)][int]$Intervalo = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$heimdallRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$heimdallDirectory = [IO.Path]::GetFullPath($Diretorio)
$heimdallPython = Join-Path $heimdallRoot '.venv\Scripts\python.exe'
$heimdallPythonWindowless = Join-Path $heimdallRoot '.venv\Scripts\pythonw.exe'
$heimdallIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$heimdallSid = $heimdallIdentity.User.Value
$heimdallHash = [Security.Cryptography.SHA256]::Create()
try {
    $heimdallDigest = $heimdallHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($heimdallDirectory.ToLowerInvariant()))
} finally { $heimdallHash.Dispose() }
$heimdallSuffix = ([BitConverter]::ToString($heimdallDigest)).Replace('-', '').Substring(0, 10)
$heimdallTaskName = 'Heimdall-Sessoes-' + $heimdallSuffix
$heimdallMarker = 'Heimdall monitor v1 | ' + $heimdallRoot + ' | ' + $heimdallDirectory

function Invoke-Heimdall {
    param([string[]]$CommandArgs)
    Push-Location -LiteralPath $heimdallRoot
    try {
        & $heimdallPython -m heimdall @CommandArgs
        if ($LASTEXITCODE -ne 0) { throw "O Heimdall terminou com código $LASTEXITCODE." }
    } finally { Pop-Location }
}

function Get-HeimdallTask {
    $found = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -eq $heimdallTaskName -and $_.TaskPath -eq '\' })
    if ($found.Count -eq 0) { return $null }
    $task = $found[0]
    # O Windows pode devolver SID, domínio\usuário ou apenas o nome local.
    try {
        if ($task.Principal.UserId -match '^S-1-') {
            $taskSid = ([Security.Principal.SecurityIdentifier]::new($task.Principal.UserId)).Value
        } else {
            $taskSid = ([Security.Principal.NTAccount]::new($task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
        }
    } catch { throw 'Não foi possível confirmar o usuário da tarefa existente.' }
    if ($task.Description -ne $heimdallMarker -or $task.Actions.Count -ne 1 -or
        $task.Actions[0].Execute -ne $heimdallPythonWindowless -or
        $task.Actions[0].WorkingDirectory -ne $heimdallRoot -or
        $task.Actions[0].Arguments -ne (New-HeimdallAction -Command 'executar').Arguments -or
        $taskSid -ne $heimdallSid) {
        throw 'Existe uma tarefa com esse nome que não corresponde a este projeto/usuário. Nada foi alterado.'
    }
    return $task
}

function New-HeimdallAction {
    param([string]$Command)
    if ($heimdallDirectory.Contains('"') -or $heimdallDirectory -match '[\r\n]') {
        throw 'Diretório de controle inválido.'
    }
    # Aspas preservam caminhos de instalação com espaços.
    $arguments = '-m heimdall monitor ' + $Command + ' --diretorio "' + $heimdallDirectory.TrimEnd('\') + '"'
    return New-ScheduledTaskAction -Execute $heimdallPythonWindowless -Argument $arguments -WorkingDirectory $heimdallRoot
}

function New-HeimdallSettings {
    return New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
}

function Show-HeimdallStatus {
    Invoke-Heimdall -CommandArgs @('monitor', 'status', '--diretorio', $heimdallDirectory)
    $task = Get-HeimdallTask
    if ($null -eq $task) { Write-Output 'Tarefa do Windows: não instalada.'; return }
    $info = Get-ScheduledTaskInfo -TaskName $heimdallTaskName -TaskPath '\'
    $neverRun = $info.LastTaskResult -eq 267011
    [pscustomobject]@{
        Tarefa = $heimdallTaskName
        Estado = $task.State
        ProximoDisparo = $(if ($task.State -eq 'Disabled') { 'nenhum: tarefa desativada' } else { $info.NextRunTime })
        UltimaExecucao = $(if ($neverRun) { 'nenhuma' } else { $info.LastRunTime })
        CodigoDaUltimaExecucao = $(if ($neverRun) { 'ainda não executada' } else { $info.LastTaskResult })
        Usuario = $task.Principal.UserId
    } | Format-List
}

if (-not (Test-Path -LiteralPath $heimdallPython) -or -not (Test-Path -LiteralPath $heimdallPythonWindowless)) {
    throw 'Ambiente Python não encontrado. Recrie .venv na raiz do projeto.'
}
$heimdallTask = Get-HeimdallTask
$heimdallPrincipal = New-ScheduledTaskPrincipal -UserId $heimdallSid -LogonType Interactive -RunLevel Limited

switch ($Acao) {
    'Preparar' {
        if ($null -ne $heimdallTask) { Disable-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' | Out-Null }
        Invoke-Heimdall -CommandArgs @('monitor', 'configurar', '--diretorio', $heimdallDirectory,
            '--perfil', (Join-Path $heimdallRoot 'config\perfil.toml'), '--intervalo', [string]$Intervalo)
        $config = Get-Content -LiteralPath (Join-Path $heimdallDirectory 'monitor.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $anchor = [DateTimeOffset]::Parse($config.anchor_at)
        $end = ([DateTimeOffset]::Parse($config.ends_before)).ToString('yyyy-MM-ddTHH:mm:sszzz')
        $timer = New-ScheduledTaskTrigger -Once -At $anchor.LocalDateTime -RepetitionInterval (New-TimeSpan -Minutes $config.interval_minutes)
        $timer.EndBoundary = $end
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $heimdallSid
        $logon.EndBoundary = $end
        $settings = New-HeimdallSettings
        $settings.Enabled = $false
        $definition = New-ScheduledTask -Action (New-HeimdallAction -Command 'executar') `
            -Trigger @($timer, $logon) -Principal $heimdallPrincipal -Settings $settings -Description $heimdallMarker
        if ($null -eq $heimdallTask) {
            Register-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' -InputObject $definition | Out-Null
        } else {
            Set-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' -Action $definition.Actions `
                -Trigger $definition.Triggers -Principal $definition.Principal -Settings $definition.Settings | Out-Null
        }
        Write-Output 'Tarefa preparada e DESATIVADA. Nenhuma consulta recorrente foi iniciada.'
        Show-HeimdallStatus
    }
    'Status' { Show-HeimdallStatus }
    'Pausar' {
        if ($null -ne $heimdallTask) { Disable-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' | Out-Null }
        Invoke-Heimdall -CommandArgs @('monitor', 'pausar', '--diretorio', $heimdallDirectory)
        Write-Output 'Próximos disparos pausados. Um ciclo já iniciado pode terminar.'
    }
    'Retomar' {
        if ($null -eq $heimdallTask) { throw 'Prepare a tarefa antes de retomar.' }
        Invoke-Heimdall -CommandArgs @('monitor', 'retomar', '--diretorio', $heimdallDirectory)
        try { Enable-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' | Out-Null }
        catch {
            Invoke-Heimdall -CommandArgs @('monitor', 'pausar', '--diretorio', $heimdallDirectory)
            throw
        }
        Show-HeimdallStatus
    }
    'Executar' {
        if ($null -eq $heimdallTask -or $heimdallTask.State -eq 'Disabled') { throw 'A tarefa precisa estar instalada e habilitada.' }
        Start-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\'
        Write-Output 'Disparo solicitado. Confira Status e monitor.log; o intervalo de espera continua sendo respeitado.'
    }
    'Remover' {
        if ($null -ne $heimdallTask) {
            Disable-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' | Out-Null
            Invoke-Heimdall -CommandArgs @('monitor', 'pausar', '--diretorio', $heimdallDirectory)
            Unregister-ScheduledTask -TaskName $heimdallTaskName -TaskPath '\' -Confirm:$false
        }
        Write-Output 'Tarefa removida. Código, credenciais, histórico e logs foram preservados.'
    }
    'Testar' {
        # Reproduz o ambiente da tarefa principal em um disparo de diagnóstico.
        $name = $heimdallTaskName + '-Diagnostico-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
        $created = $false
        $started = [DateTimeOffset]::UtcNow
        try {
            $definition = New-ScheduledTask -Action (New-HeimdallAction -Command 'diagnosticar') `
                -Principal $heimdallPrincipal -Settings (New-HeimdallSettings) -Description ($heimdallMarker + ' | diagnostico temporario')
            Register-ScheduledTask -TaskName $name -TaskPath '\' -InputObject $definition | Out-Null
            $created = $true
            Start-ScheduledTask -TaskName $name -TaskPath '\'
            $watch = [Diagnostics.Stopwatch]::StartNew()
            do {
                Start-Sleep -Milliseconds 500
                $task = Get-ScheduledTask -TaskName $name -TaskPath '\'
                $info = Get-ScheduledTaskInfo -TaskName $name -TaskPath '\'
                if ($task.State -ne 'Running' -and $info.LastRunTime -ge $started.LocalDateTime.AddSeconds(-1)) { break }
            } while ($watch.Elapsed.TotalSeconds -lt 30)
            if ($task.State -eq 'Running' -or $info.LastRunTime -lt $started.LocalDateTime.AddSeconds(-1)) {
                throw 'O diagnóstico não terminou em 30 segundos.'
            }
            if ($info.LastTaskResult -ne 0) { throw "Diagnóstico terminou com código $($info.LastTaskResult). Confira diagnostic.json e monitor.log." }
            $report = Get-Content -LiteralPath (Join-Path $heimdallDirectory 'diagnostic.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([DateTimeOffset]::Parse($report.at) -lt $started -or $report.status -ne 'ok') { throw 'O diagnóstico não produziu uma confirmação nova.' }
            $report | ConvertTo-Json
        } finally {
            if ($created) {
                $task = Get-ScheduledTask -TaskName $name -TaskPath '\'
                if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $name -TaskPath '\' }
                Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false
            }
        }
    }
}
