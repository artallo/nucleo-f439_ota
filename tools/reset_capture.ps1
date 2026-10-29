# Сброс платы по SMP (os reset) и захват консоли ST-Link через него.
# COM-порт открывается ДО сброса в том же процессе: лог MCUboot идёт сразу
# после reset, отдельным процессом его не поймать.
#
#   .\tools\reset_capture.ps1 [-Port COM10] [-Ip 192.168.7.1] [-Seconds 12] [-NoReset] [-All]
#
param(
    [string]$Port = "COM10",
    [string]$Ip = "192.168.7.1",
    [int]$Seconds = 12,
    [switch]$NoReset,   # только слушать (сброс сделает кто-то другой)
    [switch]$All        # печатать все строки, не только MCUboot/sber_ota
)

$env:PYTHONUTF8 = "1"
$smpmgr = Join-Path $PSScriptRoot "..\..\..\.venv\Scripts\smpmgr.exe"

$p = New-Object System.IO.Ports.SerialPort $Port, 115200, None, 8, One
$p.Open()
$p.DiscardInBuffer()
if (-not $NoReset) {
    $null = & $smpmgr --ip $Ip --timeout 5 os reset 2>&1
}
$sw = [Diagnostics.Stopwatch]::StartNew()
$out = ""
while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    $out += $p.ReadExisting()
    Start-Sleep -Milliseconds 50
}
$p.Close()

$lines = ($out -replace '\x1b\[[0-9;]*[A-Za-z]', '') -split "`n" |
    ForEach-Object { $_ -replace '^uart:~\$ ', '' } |
    Where-Object { $_ -match '\S' -and $_ -notmatch '^uart:~\$\s*$' }
if (-not $All) {
    $lines = $lines | Where-Object { $_ -match 'MCUboot|^I:|^W:|^E:|sber_ota|smp_udp|mcuboot' }
}
$lines
