param([string]$Python = "python", [string]$Output = "artifacts/PluginEdition")
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --onefile --windowed --name NovelPress --icon book.ico --add-data "studio.html;." --add-data "book.ico;." --add-data "VERSION;." --distpath $Output --workpath "$env:TEMP/novelpress-pybuild" --specpath "$PSScriptRoot" studio.py
    if ($LASTEXITCODE) { throw "PyInstaller exited with code $LASTEXITCODE" }
    $target = New-Item -Force -ItemType Directory (Join-Path $Output "plugins")
    Get-ChildItem plugins -File | ForEach-Object {
        $file = Join-Path $target.FullName $_.Name
        if (-not (Test-Path -LiteralPath $file)) { Copy-Item -LiteralPath $_.FullName -Destination $file }
    }
} finally {
    Pop-Location
}

