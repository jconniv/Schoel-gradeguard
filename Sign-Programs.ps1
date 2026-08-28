param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [string]$Thumbprint = "4EFF173D38E827E96A8A2C91A021D12EF5EB2CD8",
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $roots = @("${env:ProgramFiles(x86)}\Windows Kits", "$env:ProgramFiles\Microsoft Visual Studio", "${env:ProgramFiles(x86)}\Microsoft Visual Studio") |
        Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    foreach ($root in $roots) {
        $match = Get-ChildItem -LiteralPath $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($match) { return $match.FullName }
    }
    throw "signtool.exe was not found. Install the Windows SDK before building a signed release."
}

$cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
    Where-Object { $_.Thumbprint -eq $Thumbprint -and $_.HasPrivateKey } |
    Select-Object -First 1
if (-not $cert) { throw "Schoel signing certificate $Thumbprint with private key was not found in the Windows certificate store." }

$signTool = Find-SignTool
foreach ($item in $Path) {
    $target = (Get-Item -LiteralPath $item -ErrorAction Stop).FullName
    & $signTool sign /fd SHA256 /sha1 $Thumbprint /tr $TimestampServer /td SHA256 /v $target
    if ($LASTEXITCODE -ne 0) { throw "Signing failed: $target" }
    & $signTool verify /pa /v $target
    if ($LASTEXITCODE -ne 0) { throw "Signature verification failed: $target" }
}
