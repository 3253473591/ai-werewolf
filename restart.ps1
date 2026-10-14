# restart.ps1 —— 重启 AI 狼人杀服务
#
#   .\restart.ps1              重新拉起服务（前台，Ctrl+C 停止）
#   .\restart.ps1 -Port 9000   换端口
#   .\restart.ps1 -Stop        只停不启
#
# 什么时候需要重启：
#   · 改了 aiwolf/*.py       —— 必须重启（Python 模块只在启动时加载一次）
#   · 改了 static/index.html —— 不用重启，浏览器 Ctrl+F5 就行
#   · 改了 config/*.json     —— 服务每次开局都重读，但改端点建议重启更稳

param(
    [int]$Port = 8099,
    [switch]$Stop
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

function Get-Listener([int]$p) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "=== AI 狼人杀 · 重启 ===" -ForegroundColor Cyan

# ---------- 1. 停掉旧进程 ----------
$conns = Get-Listener $Port
if ($conns) {
    $procIds = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $procIds) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        $name = if ($proc) { $proc.ProcessName } else { '?' }
        Write-Host "  停止占用 $Port 的进程：PID $procId ($name)"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Listener $Port)) { break }
        Start-Sleep -Milliseconds 250
    }
}

if (Get-Listener $Port) {
    Write-Host "  ✗ 端口 $Port 仍被占用，请手动处理后再试" -ForegroundColor Red
    exit 1
}
Write-Host "  ✓ 端口 $Port 已释放"

if ($Stop) {
    Write-Host "  只停不启，结束。" -ForegroundColor Yellow
    exit 0
}

# ---------- 2. 自检：prompt 能加载吗 ----------
# 注意：这里不能用 python -c "... from ... import ..." ——
# PowerShell 5.1 的解析器会把字符串里的 from 当关键字，直接语法错误。
$env:PYTHONIOENCODING = 'utf-8'
$probe = "import sys; sys.path.insert(0,'.'); import aiwolf.render as r; print(len(r.COMMON_RULES)+len(r.PROTOCOL))"
$check = python -c $probe 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ✗ 提示词/代码加载失败，先修好再启动：" -ForegroundColor Red
    Write-Host "    $check" -ForegroundColor DarkGray
    exit 1
}
Write-Host "  ✓ 提示词加载正常（Main + 协议 共 $check 字）"

# ---------- 3. 启动 ----------
Write-Host "  → 启动服务 http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host "    （Ctrl+C 停止）" -ForegroundColor DarkGray
Write-Host ""

python -u server.py --host 127.0.0.1 --port $Port
