# Gera dist\TBMailMigrator.exe (arquivo único, sem console).
# Uso:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade -r requirements-dev.txt
if (-not (Test-Path "assets\icon.ico")) { python tools\make_icon.py }

python tests\test_core.py
if ($LASTEXITCODE -ne 0) { throw "Os testes falharam - build cancelado." }

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name TBMailMigrator `
    --icon assets\icon.ico `
    --add-data "assets\icon.ico;." `
    --version-file version_info.txt `
    --paths src `
    src\tbmigrator\__main__.py

Write-Host "`nPronto: dist\TBMailMigrator.exe" -ForegroundColor Green
