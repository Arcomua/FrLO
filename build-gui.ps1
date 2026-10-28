param([string]$Python = 'py')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonArgs = @()
if ($Python -eq 'py') { $pythonArgs = @('-3') }
& $Python @pythonArgs -m pip install 'pyinstaller>=6,<7'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed' }
& $Python @pythonArgs -m PyInstaller --noconfirm --clean --onedir --console --name FrLO-Publisher --collect-submodules frlo_release gui-entry.py
if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
Write-Host 'Created dist/FrLO-Publisher/FrLO-Publisher.exe. Keep this folder beside the publishing repository.'
