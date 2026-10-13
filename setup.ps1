<#
.SYNOPSIS
  RemoteFunnel setup for Windows.

.DESCRIPTION
  Default ("system") install, needs administrator rights:
    a SYSTEM Scheduled Task starts at boot and keeps the agent running inside whichever session
    owns the console (the approach Parsec/Sunshine use). You can then see and use the Windows
    sign-in and lock screens, UAC prompts and elevated windows, and Ctrl+Alt+Del works.
    Files go to %ProgramData%\RemoteFunnel (readable only by SYSTEM and Administrators).

  -UserMode install, no administrator rights:
    a logon task running as you, in %LOCALAPPDATA%\RemoteFunnel. The sign-in screen, UAC prompts
    and elevated windows can't be shown or controlled in this mode.

    .\setup.ps1                 install, or update an existing install (keeps password/tokens)
    .\setup.ps1 -SetPassword    new random password (add -CustomPassword to type your own)
    .\setup.ps1 -Status         show whether the agent is running and its addresses
    .\setup.ps1 -Uninstall      remove the Scheduled Task (keeps files)

  If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>
param(
  [switch]$SetPassword,
  [switch]$CustomPassword,
  [switch]$Status,
  [switch]$Uninstall,
  [string]$CloudflareToken = "",
  [string]$CloudflareHostname = "",
  [switch]$Tailscale,
  [string]$TailscaleHostname = "",
  [switch]$UserMode,          # no admin rights; sign-in screen/UAC/elevated windows not reachable
  [switch]$Yes,               # accept defaults, no questions
  [switch]$Relaunched         # internal: we re-ran ourselves elevated
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSCommandPath
$TaskName = "RemoteFunnel"
$SysDir = Join-Path $env:ProgramData "RemoteFunnel"
$UserDir = Join-Path $env:LOCALAPPDATA "RemoteFunnel"

function Say($m)  { Write-Host "==> $m" -ForegroundColor White }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!] $m" -ForegroundColor Yellow }
function Done($code) {
  if ($Relaunched) { Write-Host ""; Read-Host "Press Enter to close" | Out-Null }
  exit $code
}
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; Done 1 }
function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---- which install are we dealing with?
if ($UserMode) { $SystemMode = $false }
elseif (($Status -or $Uninstall -or $SetPassword) -and -not (Test-Path (Join-Path $SysDir "config.env")) `
        -and (Test-Path (Join-Path $UserDir "config.env"))) { $SystemMode = $false }
else { $SystemMode = $true }
if ($SystemMode) { $Dir = $SysDir } else { $Dir = $UserDir }
$Conf = Join-Path $Dir "config.env"

if ($SystemMode -and -not (Test-Admin)) {
  Say "Administrator rights are needed (for the sign-in screen, UAC prompts and Ctrl+Alt+Del)."
  Write-Host "  A UAC prompt will appear. To install without admin rights, use -UserMode instead."
  $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Relaunched")
  foreach ($kv in $PSBoundParameters.GetEnumerator()) {
    if ($kv.Value -is [System.Management.Automation.SwitchParameter]) {
      if ($kv.Value.IsPresent) { $argList += "-$($kv.Key)" }
    } else { $argList += "-$($kv.Key)"; $argList += "`"$($kv.Value)`"" }
  }
  try { $p = Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait -PassThru }
  catch { Die "elevation was cancelled (or use -UserMode)" }
  exit $p.ExitCode
}

function Ask($q, $def = "") {
  if ($Yes) { return $def }
  $suffix = ""; if ($def) { $suffix = " [$def]" }
  $a = Read-Host "$q$suffix"
  if ([string]::IsNullOrWhiteSpace($a)) { return $def } else { return $a.Trim() }
}
function YesNo($q, $def = "n") { return ((Ask "$q (y/n)" $def) -match '^[Yy]') }

# ---- KEY=VALUE config (UTF-8 without BOM, so Python reads the first key correctly)
function Read-Conf($path = $Conf) {
  $h = [ordered]@{}
  if (Test-Path $path) {
    foreach ($line in [IO.File]::ReadAllLines($path)) {
      if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2] }
    }
  }
  return $h
}
function Write-Conf($h) {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  $lines = foreach ($k in $h.Keys) { "$k=$($h[$k])" }
  [IO.File]::WriteAllLines($Conf, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
}

function Get-Sha256Hex($s) {
  $sha = [Security.Cryptography.SHA256]::Create()
  $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($s))
  return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}
function New-RandomPassword {
  $b = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  return (($b | ForEach-Object { $_.ToString("x2") }) -join "")
}
function Set-Password($conf) {
  if ($CustomPassword) {
    $sec = Read-Host "New password" -AsSecureString
    $pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    if ($pw.Length -lt 12) { Die "use at least 12 characters" }
  } else {
    $pw = New-RandomPassword
    Say "Your password (save it in a password manager; only its hash is stored):"
    Write-Host ""; Write-Host "    $pw" -ForegroundColor Cyan; Write-Host ""
  }
  $conf["RD_TOKEN_SHA256"] = Get-Sha256Hex $pw
}

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
              (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links")
}
function Winget-Install($id, [string[]]$extra = @()) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Die "winget is missing (install 'App Installer' from the Microsoft Store)" }
  Say "Installing $id"
  winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent @extra | Out-Host
  Refresh-Path
}
function Find-Exe($name, $candidates) {
  $c = Get-Command $name -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in $candidates) { $hit = Get-ChildItem -Path $p -ErrorAction SilentlyContinue | Select-Object -First 1; if ($hit) { return $hit.FullName } }
  return $null
}

function Find-Python {
  foreach ($cmd in @("py -3.12", "py -3", "python")) {
    $parts = $cmd.Split(" ")
    $rest = @()
    if ($parts.Count -gt 1) { $rest = $parts[1..($parts.Count - 1)] }   # note: [1..0] would count backwards
    try {
      $v = & $parts[0] @rest -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v) {
        $ver = [version]$v[0]
        if ($ver -ge [version]"3.10" -and $v[1] -notmatch "WindowsApps") { return $v[1] }
      }
    } catch { }
  }
  return $null
}

function Find-Ffmpeg {
  return Find-Exe "ffmpeg" @(
    (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\ffmpeg.exe"),
    (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"),
    "${env:ProgramFiles}\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe")
}
function Find-Cloudflared {
  return Find-Exe "cloudflared" @(
    "${env:ProgramFiles}\cloudflared\cloudflared.exe", "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
    (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"))
}
function Find-Tailscale { return Find-Exe "tailscale" @("${env:ProgramFiles}\Tailscale\tailscale.exe") }

function Test-Health($port) {
  try { return ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$port/healthz").Content -eq "ok") } catch { return $false }
}

function Write-Launcher($hosts) {
  $list = (($hosts | Where-Object { $_ } | ForEach-Object { '"' + $_ + '"' }) -join ", ")
  $html = [IO.File]::ReadAllText((Join-Path $Repo "launcher\RemoteFunnel.html")).Replace("/*RF_HOSTS*/", $list)
  $out = Join-Path $Repo "RemoteFunnel.html"
  [IO.File]::WriteAllText($out, $html, (New-Object Text.UTF8Encoding($false)))
  Ok "launcher written: $out (open it from anywhere, even file://)"
}

function Stop-Agent {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  # the agent and its ffmpeg/cloudflared children (the watchdog's job object usually takes them down)
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*RemoteFunnel*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# ----------------------------------------------------------------------------------------------
if ($Status) {
  $c = Read-Conf; $port = $c["RD_PORT"]; if (-not $port) { $port = "47913" }
  $mode = "user"; if ($SystemMode) { $mode = "system" }
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($t) { Ok "task ($mode mode): $($t.State)" } else { Warn "task not installed" }
  if (Test-Health $port) { Ok "server answering on 127.0.0.1:$port" } else { Warn "server not answering (logs in $Dir)" }
  Write-Host "addresses: $($c['RD_PUBLIC_URLS'])"
  Done 0
}
if ($Uninstall) {
  Stop-Agent
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Ok "Scheduled Task removed (files kept in $Dir)"
  Done 0
}
if ($SetPassword) {
  $c = Read-Conf; Set-Password $c; Write-Conf $c
  Stop-Agent; Start-ScheduledTask -TaskName $TaskName
  Ok "password changed"
  Done 0
}

$modeName = "user mode"; if ($SystemMode) { $modeName = "system mode" }
Say "RemoteFunnel for Windows ($modeName)"
$conf = Read-Conf
if ($SystemMode -and $conf.Count -eq 0 -and (Test-Path (Join-Path $UserDir "config.env"))) {
  $conf = Read-Conf (Join-Path $UserDir "config.env")      # upgrading from a user-mode install
  Ok "kept the password and tunnels from your previous (user-mode) install"
}

# ---- prerequisites
$py = Find-Python
if (-not $py) {
  if ($SystemMode) { Winget-Install "Python.Python.3.12" @("--scope", "machine") } else { Winget-Install "Python.Python.3.12" }
  $py = Find-Python
}
if (-not $py) { Die "Python 3.10+ not found after install; open a new terminal and run setup again" }
Ok "Python: $py"
$ffmpeg = Find-Ffmpeg
if (-not $ffmpeg) { Winget-Install "Gyan.FFmpeg"; $ffmpeg = Find-Ffmpeg }
if (-not $ffmpeg) { Die "ffmpeg not found after install; open a new terminal and run setup again" }
Ok "ffmpeg: $ffmpeg"
$probe = & $ffmpeg -hide_banner -filters 2>$null | Select-String "ddagrab"
if (-not $probe) { Die "this ffmpeg has no ddagrab (Desktop Duplication); install a recent full build: winget install Gyan.FFmpeg" }

# ---- tunnels
$cfToken = $conf["CF_TUNNEL_TOKEN"]; $cfHost = $conf["CF_HOSTNAME"]
if ($CloudflareToken) { $cfToken = $CloudflareToken; $cfHost = $CloudflareHostname }
elseif (-not $cfToken -and -not $Yes) {
  Write-Host ""
  Say "Cloudflare Tunnel (optional): publishes the desktop at a hostname on your own domain."
  Write-Host "  Zero Trust dashboard -> Networks -> Tunnels -> Create -> copy the install command." -ForegroundColor DarkGray
  $cfToken = Ask "  Paste the tunnel token or install command (Enter to skip)" ""
}
if ($cfToken -match '(eyJ[A-Za-z0-9_=.\-]+)') { $cfToken = $Matches[1] } elseif ($cfToken) { Warn "that doesn't contain a tunnel token; skipping Cloudflare"; $cfToken = "" }
if ($cfToken -and -not $cfHost) { $cfHost = Ask "  Public hostname routed to this tunnel (e.g. desktop.example.com)" "" }

$useTs = [bool]$conf["TS_HOSTNAME"] -or $Tailscale
if (-not $useTs -and -not $Yes) {
  Write-Host ""
  Say "Tailscale Funnel (optional): a free https://<name>.<tailnet>.ts.net address, no domain needed."
  $useTs = YesNo "  Set up Tailscale Funnel?" "n"
}

# ---- agent files + virtualenv
Stop-Agent
Say "Installing the agent into $Dir"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
foreach ($f in @("app.py", "media.py", "keymap.py", "platform_windows.py", "winsys.py", "winsvc.py",
                 "client.html", "requirements-windows.txt")) {
  Copy-Item -Force (Join-Path $Repo "server\$f") $Dir
}
$venvPy = Join-Path $Dir "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { & $py -m venv (Join-Path $Dir "venv"); if ($LASTEXITCODE) { Die "venv creation failed" } }
& $venvPy -m pip install -q --upgrade pip
& $venvPy -m pip install -q -r (Join-Path $Dir "requirements-windows.txt")
if ($LASTEXITCODE) { Die "pip install failed" }
Ok "Python packages installed"

if (-not $conf["RD_TOKEN_SHA256"]) { Set-Password $conf }
if (-not $conf["RD_PORT"]) { $conf["RD_PORT"] = "47913" }
$port = $conf["RD_PORT"]
$conf["RD_BIND"] = "127.0.0.1"
$conf["RD_FFMPEG"] = $ffmpeg
$conf["RD_LOG"] = (Join-Path $Dir "remotefunnel.log")
$urls = @()
if ($cfToken) {
  $cf = Find-Cloudflared
  if (-not $cf) { Winget-Install "Cloudflare.cloudflared"; $cf = Find-Cloudflared }
  if (-not $cf) { Die "cloudflared not found after install" }
  $conf["CF_TUNNEL_TOKEN"] = $cfToken; $conf["CF_HOSTNAME"] = $cfHost
  $conf["RD_RUN_CLOUDFLARED"] = "1"; $conf["RD_CLOUDFLARED"] = $cf
  if ($cfHost) { $urls += $cfHost }
  Ok "Cloudflare Tunnel: cloudflared is kept running for you"
} else { $conf["RD_RUN_CLOUDFLARED"] = "0" }
Write-Conf $conf

# ---- start-up task
$pythonw = Join-Path $Dir "venv\Scripts\pythonw.exe"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
if ($SystemMode) {
  # config holds the password hash and tunnel token: SYSTEM and Administrators only
  & icacls $Dir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
  # let our SYSTEM agent generate Ctrl+Alt+Del (SendSAS): 3 = services and ease-of-access apps
  New-Item -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Force | Out-Null
  Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name SoftwareSASGeneration -Value 3 -Type DWord
  Say "Registering the boot task (runs as SYSTEM)"
  $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Dir\winsvc.py`" --config `"$Conf`"" -WorkingDirectory $Dir
  $trigger = New-ScheduledTaskTrigger -AtStartup
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
  Say "Registering the logon task"
  $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Dir\app.py`" --config `"$Conf`"" -WorkingDirectory $Dir
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$up = $false
for ($i = 0; $i -lt 40 -and -not $up; $i++) { Start-Sleep 1; $up = Test-Health $port }
if ($up) { Ok "agent running on 127.0.0.1:$port" } else { Warn "agent not answering yet; see the logs in $Dir" }

# ---- Tailscale Funnel
if ($useTs) {
  $ts = Find-Tailscale
  if (-not $ts) { Winget-Install "tailscale.tailscale"; $ts = Find-Tailscale }
  if (-not $ts) { Warn "Tailscale not found; install it and run: tailscale funnel --bg $port" }
  else {
    $tsName = $TailscaleHostname; if (-not $tsName) { $tsName = $conf["TS_HOSTNAME"] }
    & $ts status *> $null
    if ($LASTEXITCODE) {
      Say "Tailscale: a browser window opens for login"
      if ($tsName) { & $ts up --hostname=$tsName } else { & $ts up }
    }
    Say "Enabling Funnel (approve in the browser if Tailscale asks)"
    & $ts funnel --bg $port
    $st = (& $ts status --json) -join "`n" | ConvertFrom-Json
    $name = $st.Self.DNSName.TrimEnd(".")
    if ($name) { $urls += $name; $conf["TS_HOSTNAME"] = $name.Split(".")[0]; Ok "Tailscale Funnel: https://$name" }
  }
}

$conf["RD_PUBLIC_URLS"] = ($urls -join ",")
Write-Conf $conf
Stop-Agent; Start-ScheduledTask -TaskName $TaskName
if ($SystemMode) {
  # a user-mode install from before would otherwise keep running at logon as well
  if (Test-Path (Join-Path $UserDir "config.env")) { Warn "an old user-mode copy is still in $UserDir; you can delete that folder" }
}
Write-Launcher $urls

Write-Host ""
Say "Done."
foreach ($u in $urls) { Write-Host "    https://$u" }
Write-Host "    or open $Repo\RemoteFunnel.html from any device"
if ($SystemMode) {
  Write-Host "  The sign-in screen, lock screen, UAC prompts and Ctrl+Alt+Del all work remotely."
} else {
  Write-Host "  User mode: the sign-in screen, UAC prompts and elevated windows can't be reached remotely."
}
Write-Host "  Tip: 'powercfg /change standby-timeout-ac 0' stops the PC from sleeping. New password: .\setup.ps1 -SetPassword"
Done 0
