$ErrorActionPreference = 'Stop'
$RootDir = Split-Path -Parent $PSScriptRoot
$DaemonJs = Join-Path $RootDir 'daemon\syn_topbar_daemon.js'

$NodeCmd = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeCmd) {
    throw 'Node.js (>= 22) is required but was not found on PATH.'
}
$NodeExe = $NodeCmd.Source

# Stop existing syn_topbar_daemon if running
Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*syn_topbar_daemon.js*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Register Windows Startup VBScript so it auto-starts on login
$StartupDir = [Environment]::GetFolderPath('Startup')
$VbsPath = Join-Path $StartupDir 'AntigravitySynDaemon.vbs'
$VbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "$RootDir"
WshShell.Run """$NodeExe"" ""$DaemonJs""", 0, False
"@
Set-Content -Path $VbsPath -Value $VbsContent -Encoding ASCII

# Launch detached via wscript.exe
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$VbsPath`"" -WorkingDirectory $RootDir

Write-Host "[antigravity-sync] Topbar 'Syn' daemon started and registered in Startup: $VbsPath"
