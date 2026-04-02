$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\HYDRA.lnk")
$shortcut.TargetPath = "C:\Users\elamj\hydra\start_all.bat"
$shortcut.WorkingDirectory = "C:\Users\elamj\hydra"
$shortcut.Description = "HYDRA Trading Agent and Dashboard"
$shortcut.Save()
Write-Host "Startup shortcut created successfully."
