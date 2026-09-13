param(
    [Parameter(Mandatory = $true)]
    [string]$Interface,
    [Parameter(Mandatory = $true)]
    [string]$Prefix,
    [int]$Port = 20043,
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [Parameter(Mandatory = $true)]
    [string]$LogPath
)

$ErrorActionPreference = "Stop"
$ruleName = "Nexus temporary /128 Agent acceptance $Port"
$ruleCreated = $false
$testExitCode = 0
$sdkRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $sdkRoot "src"
$testScript = Join-Path $sdkRoot "examples\live_windows_host_alias_acceptance.py"

Start-Transcript -Path $LogPath -Force
try {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        throw "TCP port $Port is already in use"
    }
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Action Block `
        -Protocol TCP `
        -LocalPort $Port `
        -InterfaceAlias $Interface `
        -Profile Any | Out-Null
    $ruleCreated = $true

    & $PythonPath $testScript `
        --interface $Interface `
        --prefix $Prefix `
        --port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Python live acceptance failed with exit code $LASTEXITCODE"
    }
}
catch {
    Write-Error $_
    $testExitCode = 1
}
finally {
    if ($ruleCreated) {
        Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    Stop-Transcript
}

exit $testExitCode
