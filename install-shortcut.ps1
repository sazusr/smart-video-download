# Creates the "Video Downloader" shortcut on the Desktop and in the Start menu.
# Run it again any time the shortcut gets deleted (double-click install-shortcut.bat).
$proj = $PSScriptRoot
$ws = New-Object -ComObject WScript.Shell
$places = @(
    [Environment]::GetFolderPath('Desktop'),
    [Environment]::GetFolderPath('Programs')   # Start menu -> searchable and pinnable
)
foreach ($dir in $places) {
    $s = $ws.CreateShortcut((Join-Path $dir 'Video Downloader.lnk'))
    $s.TargetPath       = Join-Path $proj 'start.bat'
    $s.WorkingDirectory = $proj
    $s.WindowStyle      = 7   # minimized
    $s.IconLocation     = (Join-Path $proj 'app.ico') + ',0'
    $s.Description      = 'YouTube / Facebook / Instagram video downloader'
    $s.Save()
    Write-Host "Created: $dir\Video Downloader.lnk"
}
