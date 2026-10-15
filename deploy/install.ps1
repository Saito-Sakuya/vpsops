# Windows: irm https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.ps1 | iex
[CmdletBinding()]
param(
    [ValidateSet('claude-code', 'codex', 'devin-desktop')]
    [string]$Client = 'codex',
    [switch]$Login
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Find-VpsopsUv {
    $command = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        return $command.Source
    }
    $defaultUv = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $defaultUv -PathType Leaf) {
        return $defaultUv
    }
    if ($env:UV_INSTALL_DIR) {
        $customUv = Join-Path $env:UV_INSTALL_DIR 'uv.exe'
        if (Test-Path -LiteralPath $customUv -PathType Leaf) {
            return $customUv
        }
    }
}

$uvPath = Find-VpsopsUv
if (-not $uvPath) {
    Write-Host 'Installing uv for the current Windows user...'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
    if ($LASTEXITCODE -ne 0) {
        throw 'uv installation failed.'
    }
    $uvPath = Find-VpsopsUv
    if (-not $uvPath) {
        throw 'uv was not found after installation. Reopen PowerShell and run this installer again.'
    }
}

# Use a source archive so Git is not required on the client computer.
$package = 'vpsops[proxy] @ https://github.com/Saito-Sakuya/vpsops/archive/refs/heads/master.tar.gz'
& $uvPath tool install --python 3.11 --upgrade $package
if ($LASTEXITCODE -ne 0) {
    throw 'vpsops-proxy installation failed.'
}
& $uvPath tool update-shell
if ($LASTEXITCODE -ne 0) {
    throw 'Could not add the uv tool directory to PATH.'
}
$toolBin = (& $uvPath tool dir --bin | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $toolBin) {
    throw 'Could not locate the uv tool directory.'
}
$proxyPath = Join-Path $toolBin 'vpsops-proxy.exe'
if (-not (Test-Path -LiteralPath $proxyPath -PathType Leaf)) {
    throw "The proxy executable was not found: $proxyPath"
}

Write-Host 'vpsops-proxy is installed. Reopen your terminal and AI client to refresh PATH.'
if ($Login) {
    & $proxyPath --client $Client login
    if ($LASTEXITCODE -ne 0) {
        throw 'Gateway login failed.'
    }
    & $proxyPath --client $Client check
    if ($LASTEXITCODE -ne 0) {
        throw 'Gateway connection check failed.'
    }
} else {
    Write-Host "Next: vpsops-proxy --client $Client login"
    Write-Host "Then: vpsops-proxy --client $Client check"
}
