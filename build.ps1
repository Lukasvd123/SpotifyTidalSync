Set-Location $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "SpotifySync Build Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path ".env")) {
    Write-Host "Creating new .env file..." -ForegroundColor Yellow
    
    $envContent = @"
# Spotify Configuration
# Get these from https://developer.spotify.com/dashboard/applications
SPOTIFY_CLIENT_ID=your_spotify_client_id_here
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback

# Application Settings
DEBUG_MODE=False
"@
    
    $envContent | Out-File -FilePath ".env" -Encoding UTF8
    
    Write-Host "[DONE] Created .env file." -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host "1. Open .env in Notepad"
    Write-Host "2. Add your Spotify Client ID and Secret"
    Write-Host "3. Run this script again"
    Write-Host ""
    pause
    exit
}

$envContent = Get-Content ".env" -Raw

if ($envContent -match "your_spotify_client_id_here") {
    Write-Host "[ERROR] Your .env file is not configured yet." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please edit .env and replace:" -ForegroundColor Yellow
    Write-Host "  - your_spotify_client_id_here"
    Write-Host "  - your_spotify_client_secret_here"
    Write-Host ""
    Write-Host "with your actual Spotify credentials from:" -ForegroundColor Yellow
    Write-Host "https://developer.spotify.com/dashboard/applications"
    Write-Host ""
    pause
    exit
}

Write-Host "Configuration OK - Starting build..." -ForegroundColor Green
Write-Host ""

pip install -r requirements.txt

if (Test-Path "SpotifySync.exe") { Remove-Item "SpotifySync.exe" -Force }
if (Test-Path "dist") { Remove-Item "dist" -Recurse -Force }
if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
if (Test-Path "SpotifySync.spec") { Remove-Item "SpotifySync.spec" -Force }

pyinstaller --noconsole --onefile --add-data ".env;." --name "SpotifySync" spotify.py

if (Test-Path "dist\SpotifySync.exe") {
    Move-Item "dist\SpotifySync.exe" "." -Force
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "BUILD SUCCESS!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "SpotifySync.exe is ready to use." -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "BUILD FAILED" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "Check the errors above." -ForegroundColor Red
    Write-Host ""
}

pause