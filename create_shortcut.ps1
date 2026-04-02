$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\HYDRA.lnk")
$repoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$shortcut.TargetPath = "$repoDir\start_all.bat"
$shortcut.WorkingDirectory = $repoDir
$shortcut.Description = "HYDRA Trading Agent and Dashboard"
$shortcut.Save()
Write-Host "Startup shortcut created successfully."
