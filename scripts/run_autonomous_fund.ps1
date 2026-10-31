param(
    [ValidateSet('mock', 'backtest', 'paper', 'report', 'stop', 'resume', 'attach-order', 'reset-drawdown')]
    [string]$Mode = 'mock',
    [string]$RunDir = 'artifacts/autonomous-fund',
    [string]$Model = '',
    [string]$Config = 'scripts/autonomous_fund/config.json',
    [string]$Start = '2025-01-06',
    [string]$End = '2025-02-14',
    [string]$DataDir = '',
    [switch]$Scripted
)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $pythonPath = Join-Path (Get-Location) '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install requirements.txt first.' }
    $runArgs = @('-m', 'scripts.autonomous_fund.run', $Mode, '--run-dir', $RunDir, '--config', $Config,
                 '--start', $Start, '--end', $End)
    if ($Model) { $runArgs += @('--model', $Model) }
    if ($DataDir) { $runArgs += @('--data-dir', $DataDir) }
    if ($Scripted) { $runArgs += '--scripted' }
    if ($Mode -in @('mock', 'backtest')) {
        New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
        $stdoutPath = Join-Path $RunDir 'stdout.log'
        $stderrPath = Join-Path $RunDir 'stderr.log'
        if ($runArgs | Where-Object { $_ -match '"' }) { throw 'Arguments cannot contain double quotes.' }
        $quotedArgs = $runArgs | ForEach-Object { '"' + $_ + '"' }
        $process = Start-Process -FilePath $pythonPath -ArgumentList $quotedArgs -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        try { Wait-Process -Id $process.Id -Timeout 1200 -ErrorAction Stop }
        catch {
            if (-not $process.HasExited) { Stop-Process -Id $process.Id }
            throw 'Backtest exceeded 1200 seconds or was interrupted; inspect run logs.'
        }
        $process.Refresh()
        Get-Content -LiteralPath $stdoutPath -Tail 55
        if ($process.ExitCode -ne 0) {
            Get-Content -LiteralPath $stderrPath -Tail 25
            throw "Runner exited with code $($process.ExitCode)."
        }
    } else {
        & $pythonPath @runArgs
        if ($LASTEXITCODE -ne 0) { throw "Runner exited with code $LASTEXITCODE." }
    }
} finally { Pop-Location }
