# Navigate to project root (parent of scripts/)
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "SpotifySync Build Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ==========================================
# STEP 1: Detect or Install Python
# ==========================================

function Find-Python {
    try {
        $ver = & python --version 2>&1
        if ($ver -match "Python 3") { return "python" }
    } catch {}

    try {
        $ver = & python3 --version 2>&1
        if ($ver -match "Python 3") { return "python3" }
    } catch {}

    try {
        $ver = & py -3 --version 2>&1
        if ($ver -match "Python 3") { return "py -3" }
    } catch {}

    $commonPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
        "$env:ProgramFiles\Python*\python.exe",
        "C:\Python*\python.exe"
    )
    foreach ($pattern in $commonPaths) {
        $found = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
        if ($found) {
            $dir = Split-Path $found.FullName
            $env:Path = "$dir;$dir\Scripts;$env:Path"
            Write-Host "Found Python at: $($found.FullName)" -ForegroundColor Yellow
            Write-Host "Note: Python was not in your PATH. Added temporarily for this build." -ForegroundColor Yellow
            return "python"
        }
    }

    return $null
}

$pythonCmd = Find-Python

if (-not $pythonCmd) {
    Write-Host "[!] Python 3 is not installed or not in PATH." -ForegroundColor Red
    Write-Host ""
    $install = Read-Host "Would you like to install Python 3 automatically? (y/n)"

    if ($install -eq 'y' -or $install -eq 'Y') {
        Write-Host ""
        Write-Host "Installing Python 3..." -ForegroundColor Yellow

        $wingetAvailable = $false
        try {
            $null = Get-Command winget -ErrorAction Stop
            $wingetAvailable = $true
        } catch {}

        if ($wingetAvailable) {
            Write-Host "Using winget to install Python..." -ForegroundColor Cyan
            winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
        } else {
            Write-Host "Downloading Python installer..." -ForegroundColor Cyan
            $pythonUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
            $installer = "$env:TEMP\python_installer.exe"

            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -Uri $pythonUrl -OutFile $installer -UseBasicParsing
            } catch {
                Write-Host "[ERROR] Failed to download Python installer: $_" -ForegroundColor Red
                Write-Host "Please install Python manually from: https://www.python.org/downloads/" -ForegroundColor Yellow
                Write-Host "IMPORTANT: Check 'Add Python to PATH' during installation!" -ForegroundColor Yellow
                pause
                exit
            }

            Write-Host "Running Python installer (this may take a moment)..." -ForegroundColor Cyan
            Start-Process -FilePath $installer -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1", "Include_test=0" -Wait

            if (Test-Path $installer) {
                Remove-Item $installer -Force -ErrorAction SilentlyContinue
            }
        }

        $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
        $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = "$userPath;$machinePath"

        $pythonCmd = Find-Python

        if (-not $pythonCmd) {
            Write-Host ""
            Write-Host "[ERROR] Python installation succeeded but could not be found." -ForegroundColor Red
            Write-Host "This usually means PATH was not updated. Please:" -ForegroundColor Yellow
            Write-Host "  1. Close this window" -ForegroundColor Yellow
            Write-Host "  2. Open a NEW PowerShell/terminal window" -ForegroundColor Yellow
            Write-Host "  3. Run this build script again" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "If it still fails, reinstall Python from https://www.python.org/downloads/" -ForegroundColor Yellow
            Write-Host "and make sure to check 'Add Python to PATH'." -ForegroundColor Yellow
            pause
            exit
        }

        Write-Host "[OK] Python installed successfully!" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "Python 3 is required to build SpotifySync." -ForegroundColor Yellow
        Write-Host "Download it from: https://www.python.org/downloads/" -ForegroundColor Yellow
        Write-Host "IMPORTANT: Check 'Add Python to PATH' during installation!" -ForegroundColor Yellow
        pause
        exit
    }
}

Write-Host ""
if ($pythonCmd -eq "py -3") {
    $pyVer = & py -3 --version 2>&1
} else {
    $pyVer = & $pythonCmd --version 2>&1
}
Write-Host "[OK] Using: $pyVer" -ForegroundColor Green

# Helper to run python commands regardless of py -3 vs python
function Run-Py {
    param([string[]]$Arguments)
    if ($pythonCmd -eq "py -3") {
        & py -3 @Arguments
    } else {
        & $pythonCmd @Arguments
    }
}

# ==========================================
# STEP 2: Verify and fix pip
# ==========================================

function Get-PipCommand {
    try {
        Run-Py -Arguments @("-m", "pip", "--version") 2>&1 | Out-Null
        return $true
    } catch {}
    return $false
}

$pipWorks = Get-PipCommand

if (-not $pipWorks) {
    Write-Host "[!] pip is not working. Attempting to fix..." -ForegroundColor Yellow

    try {
        Run-Py -Arguments @("-m", "ensurepip", "--upgrade") 2>&1
        $pipWorks = Get-PipCommand
    } catch {}

    if (-not $pipWorks) {
        Write-Host "Downloading get-pip.py..." -ForegroundColor Yellow
        $getPip = "$env:TEMP\get-pip.py"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
            Run-Py -Arguments @($getPip) 2>&1
            Remove-Item $getPip -Force -ErrorAction SilentlyContinue
            $pipWorks = Get-PipCommand
        } catch {
            Write-Host "[ERROR] Failed to install pip: $_" -ForegroundColor Red
        }
    }

    if (-not $pipWorks) {
        Write-Host "[ERROR] Could not get pip working." -ForegroundColor Red
        Write-Host "Please try: $pythonCmd -m ensurepip --upgrade" -ForegroundColor Yellow
        Write-Host "Or reinstall Python with pip included." -ForegroundColor Yellow
        pause
        exit
    }

    Write-Host "[OK] pip is now working." -ForegroundColor Green
}

# ==========================================
# STEP 3: Sync community mappings
# ==========================================

$appdataDir = "$env:APPDATA\SpotifyTidalSync"
if (-not (Test-Path $appdataDir)) {
    New-Item -ItemType Directory -Path $appdataDir -Force | Out-Null
}

$appdataMappings = "$appdataDir\mappings.json"

if (Test-Path "mappings.json") {
    Write-Host "Found community mappings.json in project folder. Merging..." -ForegroundColor Cyan

    if (Test-Path $appdataMappings) {
        try {
            $newData = Get-Content "mappings.json" -Raw | ConvertFrom-Json
            $existing = Get-Content $appdataMappings -Raw | ConvertFrom-Json

            $merged = @{}
            $existing.PSObject.Properties | ForEach-Object { $merged[$_.Name] = $_.Value }

            $added = 0
            $newData.PSObject.Properties | ForEach-Object {
                if (-not $merged.ContainsKey($_.Name)) {
                    $merged[$_.Name] = $_.Value
                    $added++
                }
            }

            $merged | ConvertTo-Json -Depth 10 | Out-File $appdataMappings -Encoding UTF8
            Write-Host "[OK] Merged $added new community corrections into appdata." -ForegroundColor Green
        } catch {
            Write-Host "[WARNING] Could not merge mappings: $_" -ForegroundColor Yellow
            Copy-Item "mappings.json" $appdataMappings -Force
        }
    } else {
        Copy-Item "mappings.json" $appdataMappings
        Write-Host "[OK] Copied community mappings to appdata." -ForegroundColor Green
    }

    Remove-Item "mappings.json" -Force
    Write-Host "[OK] Removed mappings.json from project folder (now in appdata)." -ForegroundColor Green
}

Write-Host ""

# ==========================================
# STEP 5: Install dependencies
# ==========================================

Write-Host "Installing dependencies..." -ForegroundColor Cyan

Run-Py -Arguments @("-m", "pip", "install", "-r", "requirements.txt")

if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARNING] Some dependencies may have failed to install." -ForegroundColor Yellow
    Write-Host "The build will continue, but the exe may not work correctly." -ForegroundColor Yellow
}

Write-Host "Installing optional Windows audio control (pycaw) and media detection (winrt)..." -ForegroundColor Cyan
Run-Py -Arguments @("-m", "pip", "install", "pycaw", "winrt-Windows.Media.Control", "winrt-Windows.Foundation") 2>&1 | Out-Null

Write-Host ""

# ==========================================
# STEP 6: Convert logo to .ico
# ==========================================

$iconFlag = ""

if (Test-Path "assets\logo.png") {
    Write-Host "Converting logo for executable icon..." -ForegroundColor Cyan

    $convertScript = "from PIL import Image; img = Image.open('assets/logo.png'); img.save('assets/logo.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

    try {
        Run-Py -Arguments @("-c", $convertScript)

        if (Test-Path "assets\logo.ico") {
            Write-Host "[OK] Logo converted to .ico" -ForegroundColor Green
            $iconFlag = "--icon=$projectRoot\assets\logo.ico"
        }
    } catch {
        Write-Host "[WARNING] Could not convert logo: $_" -ForegroundColor Yellow
        Write-Host "  Building without custom icon." -ForegroundColor Yellow
    }
} else {
    Write-Host "[NOTE] No logo.png found in assets/. Building without custom icon." -ForegroundColor Yellow
}

Write-Host ""

# ==========================================
# STEP 7: Clean previous builds
# ==========================================

Write-Host "Cleaning previous builds..." -ForegroundColor Cyan

if (Test-Path "SpotifySync.exe") { Remove-Item "SpotifySync.exe" -Force }
if (Test-Path "_build") { Remove-Item "_build" -Recurse -Force }

# ==========================================
# STEP 8: Build executable
# ==========================================

Write-Host "Building executable..." -ForegroundColor Cyan

New-Item -ItemType Directory -Path "_build" -Force | Out-Null

$logoAsset = "$projectRoot\assets\logo.png"

# Build PyInstaller command with absolute paths (avoids splatting issues)
$pyiArgs = @(
    "-m", "PyInstaller",
    "--noconsole",
    "--onefile",
    "--name", "SpotifySync",
    "--distpath", "$projectRoot\_build\dist",
    "--workpath", "$projectRoot\_build\build",
    "--specpath", "$projectRoot\_build"
)

# Bundle logo for window icon
if (Test-Path $logoAsset) {
    $pyiArgs += @("--add-data", "$logoAsset;assets")
}

if ($iconFlag) {
    $pyiArgs += $iconFlag
}

$pyiArgs += "spotify.py"

# Invoke directly instead of Run-Py to preserve path arguments
if ($pythonCmd -eq "py -3") {
    & py -3 @pyiArgs
} else {
    & $pythonCmd @pyiArgs
}

if (-not (Test-Path "$projectRoot\_build\dist\SpotifySync.exe")) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "BUILD FAILED" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "Check the errors above." -ForegroundColor Red
    Write-Host ""
    pause
    exit
}

Move-Item "$projectRoot\_build\dist\SpotifySync.exe" "$projectRoot" -Force

# ==========================================
# STEP 9: Code Signing (self-signed)
# ==========================================

Write-Host ""
Write-Host "Signing executable..." -ForegroundColor Cyan

$certName = "SpotifyTidalSync"

try {
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
            Where-Object { $_.Subject -match $certName } |
            Select-Object -First 1

    if (-not $cert) {
        Write-Host "Creating self-signed code signing certificate..." -ForegroundColor Yellow
        $cert = New-SelfSignedCertificate `
            -Subject "CN=$certName" `
            -Type CodeSigningCert `
            -CertStoreLocation Cert:\CurrentUser\My `
            -NotAfter (Get-Date).AddYears(5)

        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
        $store.Open("ReadWrite")
        $store.Add($cert)
        $store.Close()

        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store("TrustedPublisher", "CurrentUser")
        $store.Open("ReadWrite")
        $store.Add($cert)
        $store.Close()
    }

    $result = Set-AuthenticodeSignature -FilePath "SpotifySync.exe" -Certificate $cert -TimestampServer "http://timestamp.digicert.com" -ErrorAction Stop

    if ($result.Status -eq "Valid") {
        Write-Host "[OK] Executable signed successfully." -ForegroundColor Green
    } else {
        Write-Host "[WARNING] Signing completed with status: $($result.Status)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "[NOTE] Code signing skipped: $_" -ForegroundColor Yellow
    Write-Host "  The exe will work fine, but may trigger SmartScreen warnings." -ForegroundColor Yellow
    Write-Host "  For full trust, an EV code signing certificate is needed." -ForegroundColor Yellow
}

# Clean up generated .ico after build
if (Test-Path "assets\logo.ico") {
    Remove-Item "assets\logo.ico" -Force
}

# ==========================================
# DONE
# ==========================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "BUILD SUCCESS!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "SpotifySync.exe is ready to use." -ForegroundColor Green
Write-Host ""
Write-Host "Note on code signing:" -ForegroundColor Yellow
Write-Host "  A self-signed certificate has been used. This reduces some" -ForegroundColor Yellow
Write-Host "  antivirus false positives but will NOT bypass Windows SmartScreen" -ForegroundColor Yellow
Write-Host "  or corporate security policies. For full trust, purchase an EV" -ForegroundColor Yellow
Write-Host "  code signing certificate from a CA like DigiCert or Sectigo." -ForegroundColor Yellow
Write-Host ""

pause
