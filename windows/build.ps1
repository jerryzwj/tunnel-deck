# 构建 Windows 原生版 TunnelDeck.exe（不依赖 Docker）
# 用法：powershell -ExecutionPolicy Bypass -File windows\build.ps1
$ErrorTrigger = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $root

$cf = Join-Path $root 'windows\cloudflared.exe'
if (-not (Test-Path -LiteralPath $cf)) {
    Write-Host '下载 cloudflared.exe ...'
    $env:HTTPS_PROXY = 'http://127.0.0.1:7897'
    Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cf -UseBasicParsing
}

python (Join-Path $root 'windows\make_icon.py')

python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name TunnelDeck `
  --icon (Join-Path $root 'windows\icon.ico') `
  --distpath (Join-Path $root 'windows\dist') `
  --workpath (Join-Path $root 'windows\build') `
  --specpath (Join-Path $root 'windows') `
  --add-data "$root\web;web" `
  --add-data "$cf;." `
  --hidden-import app.main --hidden-import app.cf --hidden-import app.db `
  --hidden-import app.net --hidden-import app.runner --hidden-import app.scan `
  --collect-submodules webview --collect-submodules pystray `
  --paths $root `
  (Join-Path $root 'windows\tunneldeck_win.py')

$exe = Join-Path $root 'windows\dist\TunnelDeck.exe'
if (Test-Path -LiteralPath $exe) {
    $size = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 1)
    Write-Host "OK: $exe ($size MB)"
} else {
    throw '构建失败：没有生成 TunnelDeck.exe'
}
