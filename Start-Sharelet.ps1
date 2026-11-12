param([switch]$Local, [int]$ShareletPort = 9271)
$ErrorActionPreference = 'Stop'
$shareletRoot = $PSScriptRoot

function Open-ShareletEntry([string]$AccessPath) {
    if (-not (Test-Path -LiteralPath $AccessPath)) { return $false }
    try {
        $entry = Get-Content -Raw -LiteralPath $AccessPath | ConvertFrom-Json
        $uri = [Uri]$entry.owner_url
        if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1','localhost')) { return $false }
        $health = Invoke-RestMethod -Uri ($uri.GetLeftPart([UriPartial]::Authority) + '/healthz') -TimeoutSec 2
        if ($health.ok -and -not $health.guest_only -and $health.version -in @('0.4.2','0.4.3','0.4.4','0.4.5','0.4.6','0.5.0','0.5.1','0.5.2')) {
            Start-Process -FilePath $entry.owner_url
            return $true
        }
    } catch { }
    return $false
}

# The prepared server preview contains a private creator entry, never a provider key.
if (-not $Local -and (Open-ShareletEntry (Join-Path $shareletRoot '.preview-v4-access.json'))) { exit 0 }

$runtimeRoot = Join-Path $shareletRoot '.state/desktop'
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$accessPath = Join-Path $runtimeRoot 'preview-access.json'
if (Open-ShareletEntry $accessPath) { exit 0 }

$privateVenv = Join-Path $shareletRoot '.venv/Scripts/python.exe'
if (Test-Path -LiteralPath $privateVenv) { $pythonCommand = $privateVenv }
else { $pythonCommand = (Get-Command python).Source }
$arguments = @(('"' + (Join-Path $shareletRoot 'app.py') + '"'), '--mode', 'local',
               '--port', $ShareletPort, '--guest-port', ($ShareletPort + 1), '--data-dir', ('"' + $runtimeRoot + '"'))
$taskProcess = Start-Process -FilePath $pythonCommand -ArgumentList $arguments -WorkingDirectory $shareletRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeRoot 'preview.log') -RedirectStandardError (Join-Path $runtimeRoot 'preview-error.log')
$taskProcess.Id | Set-Content -LiteralPath (Join-Path $runtimeRoot 'preview.pid')
for ($attempt = 0; $attempt -lt 50; $attempt++) {
    Start-Sleep -Milliseconds 200
    if ($taskProcess.HasExited) { throw ('Sharelet stopped. See ' + (Join-Path $runtimeRoot 'preview-error.log')) }
    if (Open-ShareletEntry $accessPath) { exit 0 }
}
throw ('Sharelet is still starting. See ' + (Join-Path $runtimeRoot 'preview.log'))
