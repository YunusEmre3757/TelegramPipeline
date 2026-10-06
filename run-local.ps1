param(
    [ValidateSet('login', 'backfill', 'live', 'health', 'export-json', 'fetch-articles')]
    [string]$Mode = 'live',
    [string]$Channel = '@ekonomi',
    [ValidateRange(1, 3650)]
    [int]$Days = 2,
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$Since,
    [ValidateRange(1, 2147483647)]
    [int]$Limit,
    [switch]$Retry
)

$ErrorActionPreference = 'Stop'
$pilotRoot = Join-Path $PSScriptRoot 'local-pilot'
$pilotPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pilotPython)) {
    throw 'Once python -m venv .venv ve .\.venv\Scripts\python.exe -m pip install -r requirements.txt komutlarini calistirin.'
}
if ($Channel -match '[\r\n#]' -or [string]::IsNullOrWhiteSpace($Channel)) {
    throw 'Channel tek bir kanal adi veya ID olmali.'
}
$pilotEnvNames = @('TG_DB', 'TG_SESSION', 'TG_CHANNELS', 'TG_LOG_FILE', 'TG_HEALTH_FILE', 'TG_API_ID', 'TG_API_HASH', 'PYTHONUTF8', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE')
$pilotPreviousEnv = @{}
foreach ($pilotName in $pilotEnvNames) {
    $pilotPreviousEnv[$pilotName] = [Environment]::GetEnvironmentVariable($pilotName, 'Process')
}
try {
    New-Item -ItemType Directory -Path $pilotRoot -Force | Out-Null
    $env:TG_DB = Join-Path $pilotRoot 'archive.db'
    $env:TG_SESSION = Join-Path $pilotRoot 'telegram'
    $env:TG_CHANNELS = Join-Path $pilotRoot 'channels.txt'
    $env:TG_LOG_FILE = Join-Path $pilotRoot 'collector.log'
    $env:TG_HEALTH_FILE = Join-Path $pilotRoot 'health.json'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'  # No __pycache__ folders beside the sources.
    # The collector is a package module; make it importable from any working directory.
    $env:PYTHONPATH = if ($env:PYTHONPATH) { $PSScriptRoot + [IO.Path]::PathSeparator + $env:PYTHONPATH } else { $PSScriptRoot }
    # Reuse the pilot list, which may contain multiple channel IDs.
    # Do not rewrite it in read-only commands or while a collector holds a lock.
    if ($Mode -in @('backfill', 'live')) {
        if (Test-Path -LiteralPath $env:TG_CHANNELS) {
            $pilotExistingChannel = (Get-Content -LiteralPath $env:TG_CHANNELS -Raw).Trim()
            if ($PSBoundParameters.ContainsKey('Channel') -and $pilotExistingChannel -ne $Channel.Trim()) {
                throw 'Kanal listesi farkli veya birden fazla kanal iceriyor. Mevcut listeyle calismak icin -Channel parametresini kullanmayin.'
            }
        } else {
            [IO.File]::WriteAllText($env:TG_CHANNELS, $Channel.Trim() + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        }
    }
    if ($Mode -notin @('health', 'export-json', 'fetch-articles')) {
        $pilotCredentialsPath = Join-Path $pilotRoot 'api.credentials.xml'
        if (Test-Path -LiteralPath $pilotCredentialsPath) {
            $pilotCredentials = Import-Clixml -LiteralPath $pilotCredentialsPath
            if (-not $env:TG_API_ID) { $env:TG_API_ID = $pilotCredentials.UserName }
            if (-not $env:TG_API_HASH) { $env:TG_API_HASH = $pilotCredentials.GetNetworkCredential().Password }
        }
        if (-not $env:TG_API_ID) { $env:TG_API_ID = Read-Host 'Telegram API ID' }
        if (-not $env:TG_API_HASH) {
            $pilotSecret = Read-Host 'Telegram API hash (gizli)' -AsSecureString
            try { $env:TG_API_HASH = [Net.NetworkCredential]::new('', $pilotSecret).Password }
            finally { $pilotSecret.Dispose() }
        }
    }
    $pilotArgs = @('-m', 'news_archive.tg_collector', $Mode)
    if ($Mode -in @('backfill', 'live')) {
        if ($Since) { $pilotArgs += @('--since', $Since) }
        elseif ($PSBoundParameters.ContainsKey('Days')) {
            $pilotArgs += @('--since', [DateTime]::UtcNow.AddDays(-$Days).ToString('yyyy-MM-dd'))
        }
    }
    if ($Mode -eq 'live') { $pilotArgs += @('--json-dir', (Join-Path $pilotRoot 'sources')) }
    if ($Mode -eq 'fetch-articles') {
        $pilotArgs += @('--json-dir', (Join-Path $pilotRoot 'sources'))
        if ($PSBoundParameters.ContainsKey('Limit')) { $pilotArgs += @('--limit', $Limit) }
        if ($Retry) { $pilotArgs += '--retry' }
    }
    if ($Mode -eq 'export-json') { $pilotArgs += @('--out-dir', (Join-Path $pilotRoot 'sources')) }
    & $pilotPython @pilotArgs
    $pilotExit = $LASTEXITCODE
    if ($pilotExit -ne 0) { throw "Toplayici cikis kodu: $pilotExit" }
} finally {
    foreach ($pilotName in $pilotEnvNames) {
        [Environment]::SetEnvironmentVariable($pilotName, $pilotPreviousEnv[$pilotName], 'Process')
    }
}
