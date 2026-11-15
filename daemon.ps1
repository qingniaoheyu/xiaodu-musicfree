# ============================================================
#  Xiaodu x MusicFree daemon
#  Monitors python service and cloudflared tunnel, auto-restarts them,
#  and syncs the new tunnel URL to the service.
# ============================================================

$ErrorActionPreference = 'SilentlyContinue'

$ROOT = 'C:\Users\Administrator\projects\xiaodu-musicfree'
$CF = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
$LOGDIR = Join-Path $ROOT 'logs'
$CFLOG = Join-Path $LOGDIR 'cloudflared.err'
$SRVOUT = Join-Path $LOGDIR 'server.out'
$SRVERR = Join-Path $LOGDIR 'server.err'
$CURRENT_URL_FILE = Join-Path $LOGDIR 'current_url.txt'
$PORT = 8765

if (-not (Test-Path $LOGDIR)) {
    New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null
}

function Write-Log {
    param([string]$msg)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts  $msg" | Out-File -FilePath (Join-Path $LOGDIR 'daemon.log') -Append -Encoding utf8
}

function Test-Port {
    param([int]$port)
    $r = Test-NetConnection -ComputerName '127.0.0.1' -Port $port -WarningAction SilentlyContinue -InformationLevel Quiet
    return $r
}

function Start-Server {
    $alive = Test-Port $PORT
    if (-not $alive) {
        Write-Log 'server not listening, starting python'
        $serverPy = Join-Path $ROOT 'bridge\server.py'
        $configJson = Join-Path $ROOT 'bridge\config.json'
        $workDir = Join-Path $ROOT 'bridge'
        Start-Process -FilePath 'python' -ArgumentList $serverPy,'--config',$configJson,'--watch-tunnel' -WorkingDirectory $workDir -WindowStyle Hidden -RedirectStandardOutput $SRVOUT -RedirectStandardError $SRVERR
    }
}

function Start-Tunnel {
    $proc = Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Log 'cloudflared not running, starting tunnel'
        $target = "http://127.0.0.1:$PORT"
        $cfOut = Join-Path $LOGDIR 'cf.out'
        Start-Process -FilePath $CF -ArgumentList 'tunnel','--url',$target,'--no-autoupdate' -WindowStyle Hidden -RedirectStandardOutput $cfOut -RedirectStandardError $CFLOG
    }
}

function Start-VoiceBridge {
    # voice bridge: poll history, search, push to xiaodu
    $bridge = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.CommandLine -match 'voice_bridge\.py'
    }
    if (-not $bridge) {
        Write-Log 'voice_bridge not running, starting'
        $vbPy = Join-Path $ROOT 'voice_bridge.py'
        $vbOut = Join-Path $LOGDIR 'voice_bridge.log'
        $vbErr = Join-Path $LOGDIR 'voice_bridge.err'
        Start-Process -FilePath 'python' -ArgumentList '-u',$vbPy -WorkingDirectory $ROOT -WindowStyle Hidden -RedirectStandardOutput $vbOut -RedirectStandardError $vbErr
    }
}

function Get-TunnelUrl {
    $url = $null
    if (Test-Path $CFLOG) {
        $matches = Select-String -Path $CFLOG -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com'
        if ($matches) {
            $last = $matches | Select-Object -Last 1
            $m = [regex]::Match($last.Line, 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($m.Success) {
                $url = $m.Value
            }
        }
    }
    return $url
}

function Sync-BaseUrl {
    $url = Get-TunnelUrl
    if (-not $url) {
        return
    }
    $url | Out-File -FilePath $CURRENT_URL_FILE -Encoding ascii
    try {
        $body = @{ base = $url } | ConvertTo-Json -Compress
        Invoke-WebRequest -Uri "http://127.0.0.1:$PORT/api/set-base" -Method POST -Body $body -ContentType 'application/json' -TimeoutSec 8 | Out-Null
    }
    catch {
        # ignore sync failure, retry next loop
    }
}

Write-Log '===== daemon started ====='

# First boot: kill leftovers
Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Start-Server
Start-Sleep -Seconds 4
Start-Tunnel
Start-Sleep -Seconds 10
Sync-BaseUrl
Start-VoiceBridge

# Main loop: check every 15 seconds
while ($true) {
    Start-Server
    Start-Tunnel
    Sync-BaseUrl
    Start-VoiceBridge
    Start-Sleep -Seconds 15
}
