param([string]$InstallDir)
$machPath = [Environment]::GetEnvironmentVariable("Path", "Machine")
if ($machPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$machPath;$InstallDir", "Machine")
    Write-Host "Added $InstallDir to PATH"
} else {
    Write-Host "$InstallDir already in PATH"
}
