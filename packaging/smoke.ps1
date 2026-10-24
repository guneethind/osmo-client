# Starts a built program and checks it works. Used by CI and by hand:
#   .\packaging\smoke.ps1 -Folder dist\OsmoClient
#   .\packaging\smoke.ps1 -Folder C:\path\to\unzipped\OsmoClient-1.6.5     (a portable copy, any depth)
# It never touches your cameras (starts with --off) and keeps its data in a temporary folder.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Folder, [int]$Port = 18091, [string]$Exe = 'OsmoClient.exe')
$ErrorActionPreference = 'Stop'
$program = Join-Path (Resolve-Path $Folder) $Exe
$failures = @()
function Check([bool]$ok, [string]$what) {
    if ($ok) { Write-Host "  ok   $what" } else { Write-Host "  FAIL $what" -ForegroundColor Red; $script:failures += $what }
}
function Get-Http([string]$path, [hashtable]$headers = @{}, [string]$method = 'GET') {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port$path" -Headers $headers -Method $method -UseBasicParsing -TimeoutSec 5
        return @{ Status = [int]$r.StatusCode; Length = $r.RawContentLength }
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { return @{ Status = [int]$_.Exception.Response.StatusCode; Length = 0 } }
        return @{ Status = 0; Length = 0 }
    }
}

# Data goes to a temporary folder: beside the program in portable mode, else LOCALAPPDATA is redirected.
$data = Join-Path ([IO.Path]::GetTempPath()) "osmo-smoke-$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Force $data | Out-Null
$env:LOCALAPPDATA = $data
$portable = Test-Path (Join-Path (Split-Path $program) 'portable.txt')
$logFolder = if ($portable) { Join-Path (Split-Path $program) 'data\logs' } else { Join-Path $data 'osmo-pc\logs' }

Write-Host "Starting $program"
$process = Start-Process $program -ArgumentList '--off', '--no-browser', '--port', $Port -PassThru -WindowStyle Hidden
try {
    $up = $false
    for ($i = 0; $i -lt 60 -and -not $up; $i++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) { break }
        $up = (Get-Http '/api/status').Status -eq 200
    }
    Check $up 'answers /api/status within 30 s'
    if ($up) {
        $page = Get-Http '/'
        Check ($page.Status -eq 200 -and $page.Length -gt 100000) 'serves the page'
        Check ((Get-Http '/monitor.js').Status -eq 200) 'serves monitor.js'
        # curl: Windows PowerShell's Invoke-WebRequest does not let a script set the Host header.
        Check ((curl.exe -s -o NUL -w '%{http_code}' -H 'Host: evil.example' "http://127.0.0.1:$Port/") -eq '403') 'refuses a foreign Host'
        Check ((curl.exe -s -o NUL -w '%{http_code}' -X POST -H 'Origin: https://evil.example' "http://127.0.0.1:$Port/api/quit") -eq '403') 'refuses a foreign Origin'
        Check ((Get-Http '/api/cameras').Status -eq 200) 'lists cameras'
        # A second copy must not start: it opens the page of the first and exits.
        $second = Start-Process $program -ArgumentList '--off', '--no-browser', '--port', $Port -PassThru -WindowStyle Hidden
        Check ($second.WaitForExit(15000)) 'a second copy exits by itself'
        if (-not $second.HasExited) { $second.Kill() }
        $quit = Get-Http '/api/quit' @{ Origin = "http://127.0.0.1:$Port" } 'POST'
        Check ($quit.Status -eq 200) 'POST /api/quit answers'
        Check ($process.WaitForExit(20000)) 'exits after quit'
        if ($process.HasExited) { Check ($process.ExitCode -eq 0) "exit code 0 (was $($process.ExitCode))" }
    }
} finally {
    if (-not $process.HasExited) { $process.Kill() }
}
$log = Join-Path $logFolder 'osmo-client.log'
if (Test-Path $log) { Check (-not (Select-String -Path $log -Pattern 'Traceback' -Quiet)) 'no traceback in the log' }
if ($failures.Count) {
    Write-Host "$($failures.Count) check(s) failed"
    if (Test-Path $log) { Get-Content $log -Tail 30 }
    exit 1
}
Write-Host 'Smoke test passed'
Remove-Item -Recurse -Force $data -ErrorAction SilentlyContinue
