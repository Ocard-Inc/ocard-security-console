# 停止所有 Security Log Console 伺服器 process 後重新啟動。
# Git Bash 的 pkill 抓不到 uv 啟動的子 process，需用 WMI 比對 CommandLine。
param([switch]$StopOnly)

$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*console.api.app*' }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force
    Write-Output "stopped pid $($p.ProcessId)"
}
Start-Sleep -Seconds 2

if ($StopOnly) { return }

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'src'
$env:PYTHONUTF8 = '1'
Start-Process -FilePath 'uv' `
    -ArgumentList 'run', 'python', '-m', 'console.api.app' `
    -WorkingDirectory $root `
    -RedirectStandardOutput (Join-Path $root 'state\logs\server.out') `
    -RedirectStandardError (Join-Path $root 'state\logs\server.err') `
    -WindowStyle Hidden
Write-Output "started; logs at state\logs\server.out"
