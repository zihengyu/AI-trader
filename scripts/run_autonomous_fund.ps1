param(
    [ValidateSet('start', 'mock', 'backtest', 'paper', 'local', 'check', 'report', 'stop', 'resume', 'attach-order', 'reset-drawdown', 'adjust-reserve', 'validate-risk', 'factor-lab', 'acceptance', 'show-config', 'schwab-check', 'serve', 'approvals', 'approve', 'reject', 'monitor', 'notify-test', 'qualify')]
    # 'start' (default) runs FUND_MODE from .env: local or paper. No live mode exists.
    [string]$Mode = 'start',
    # Empty: FUND_RUN_DIR from .env, else artifacts/fund-<mode> (historical runs: artifacts/autonomous-fund).
    [string]$RunDir = '',
    [string]$Model = '',
    [string]$ResearchModel = '',
    [string]$DecisionModel = '',
    [string]$Config = 'scripts/autonomous_fund/config.json',
    [string]$Start = '2025-01-06',
    [string]$End = '2025-02-14',
    [string]$DataDir = '',
    [switch]$Scripted,
    [switch]$Once,
    [switch]$ObserveOnly,
    [string]$EnvFile = '',
    [string]$ApprovalId = '',
    [string]$ApprovalDigest = '',
    [switch]$ConsoleOnly,
    [string]$MemoryFile = '',
    [string]$Amount = '',
    [string]$Targets = '',
    [string]$Symbols = '',
    [string]$Expression = '',
    [int]$Horizon = 0,
    [int]$MinNames = 0,
    [switch]$RebaselineExternal
)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $pythonPath = Join-Path (Get-Location) '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install requirements.txt first.' }
    $runArgs = @('-m', 'scripts.autonomous_fund.run', $Mode, '--config', $Config, '--start', $Start, '--end', $End)
    if ($RunDir) { $runArgs += @('--run-dir', $RunDir) }
    if ($Model) { $runArgs += @('--model', $Model) }
    if ($ResearchModel) { $runArgs += @('--research-model', $ResearchModel) }
    if ($DecisionModel) { $runArgs += @('--decision-model', $DecisionModel) }
    if ($DataDir) { $runArgs += @('--data-dir', $DataDir) }
    if ($Scripted) { $runArgs += '--scripted' }
    if ($Once) { $runArgs += '--once' }
    if ($ObserveOnly) { $runArgs += '--observe-only' }
    # Like the Linux launcher: explicitly select this checkout's .env when present.
    # The file is parsed (never executed); process environment values take precedence.
    if (-not $EnvFile) {
        $checkoutEnv = Join-Path (Get-Location) '.env'
        if (Test-Path -LiteralPath $checkoutEnv) { $EnvFile = $checkoutEnv }
    }
    if ($EnvFile) { $runArgs += @('--env-file', $EnvFile) }
    if ($ApprovalId) { $runArgs += @('--approval-id', $ApprovalId) }
    if ($ApprovalDigest) { $runArgs += @('--approval-digest', $ApprovalDigest) }
    if ($ConsoleOnly) { $runArgs += '--console-only' }
    if ($MemoryFile) { $runArgs += @('--memory-file', $MemoryFile) }
    if ($Amount) { $runArgs += @('--amount', $Amount) }
    if ($Targets) { $runArgs += @('--targets', $Targets) }
    if ($Symbols) { $runArgs += @('--symbols', $Symbols) }
    # One token: an expression may start with '-' (for example -vol(60)).
    if ($Expression) { $runArgs += "--expression=$Expression" }
    if ($Horizon -gt 0) { $runArgs += @('--horizon', $Horizon) }
    if ($MinNames -gt 0) { $runArgs += @('--min-names', $MinNames) }
    if ($RebaselineExternal) { $runArgs += '--rebaseline-external' }
    if ($Mode -in @('mock', 'backtest', 'qualify')) {
        if (-not $RunDir) { $RunDir = 'artifacts/autonomous-fund' }  # Same default as the Python runner.
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
