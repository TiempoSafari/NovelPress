param([string]$Python = "python", [string]$Wix = "wix")
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $v = (Get-Content VERSION -Raw).Trim()
    & ./build.ps1 -Python $Python -Output artifacts/staging
    $compiler = Join-Path $env:WINDIR "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    & $compiler /nologo /target:winexe /optimize+ /reference:System.Windows.Forms.dll "/out:$PSScriptRoot\artifacts\staging\UpdateRunner.exe" "$PSScriptRoot\installer\UpdateRunner.cs"
    if ($LASTEXITCODE) { throw "Updater build failed ($LASTEXITCODE)" }
    & $Wix build -arch x64 -culture zh-CN -ext WixToolset.UI.wixext -d "Version=$v" installer/package.wxs -o "artifacts/NovelPress-$v-x64.msi"
    if ($LASTEXITCODE) { throw "WiX failed ($LASTEXITCODE)" }
} finally { Pop-Location }

