param([switch]$SkipBuild)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$version = python -c "import schoel_gradeguard; print(schoel_gradeguard.APP_VERSION)"
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read GradeGuard version." }
$version = $version.Trim()
$exeName = "SchoelGradeGuard_v$($version.Replace('.', '_')).exe"
$exePath = Join-Path $root "dist\$exeName"
$outDir = Join-Path $root "installer_out"
$msiPath = Join-Path $outDir "SchoelGradeGuard-$version.msi"

if (-not $SkipBuild) {
    & pyinstaller --noconfirm --onefile --windowed --name ([IO.Path]::GetFileNameWithoutExtension($exeName)) --icon gradeguard_icon.ico --add-data "gradeguard_logo.png;." --add-data "gradeguard_icon.png;." --add-data "gradeguard_icon.ico;." schoel_gradeguard.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}
if (-not (Test-Path -LiteralPath $exePath)) { throw "Missing EXE: $exePath" }

& "$root\Sign-Programs.ps1" -Path $exePath
if ($LASTEXITCODE -ne 0) { throw "EXE signing failed." }

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
& wix build installer\Product.wxs -d ProductVersion=$version -d MainExePath="$exePath" -o "$msiPath"
if ($LASTEXITCODE -ne 0) { throw "WiX MSI build failed." }
& "$root\Sign-Programs.ps1" -Path $msiPath
if ($LASTEXITCODE -ne 0) { throw "MSI signing failed." }

$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $msiPath).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    name = "Schoel GradeGuard"
    version = $version
    msi = [IO.Path]::GetFileName($msiPath)
    sha256 = $hash
    signer_thumbprint = "4EFF173D38E827E96A8A2C91A021D12EF5EB2CD8"
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outDir "version.json") -Encoding UTF8
Write-Host "Built and signed $msiPath"
Write-Host "SHA256: $hash"
