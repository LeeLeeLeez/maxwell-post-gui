# Maxwell 后处理 GUI —— 一键安装（PowerShell 入口）
# 用法：右键本文件 → “使用 PowerShell 运行”
#      或终端里： powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Find-Python {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @("python") }
    if (Get-Command py     -ErrorAction SilentlyContinue) { return @("py", "-3") }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host ""
    Write-Host "[ERROR] 没找到 Python。请先安装 Python 3.9+（勾选 Add to PATH 和 tcl/tk and IDLE）："
    Write-Host "        https://www.python.org/downloads/"
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

Write-Host "使用解释器: $($py -join ' ')"
& $py[0] $py[1..($py.Length - 1)] install.py

Write-Host ""
Read-Host "按回车退出"
