<# Validated database dump plus permanent asset archives. No production restart.
   Only manifests made by this script authorize 14-day retention cleanup.
   Recovered dumps and older/manual backups are never retention targets. #>
[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()][string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [ValidateNotNullOrEmpty()][string]$BackupDirectory = 'D:\testova_backups',
    [ValidateRange(1,3650)][int]$KeepDays = 14
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath($RepositoryRoot)
$backupRoot = [IO.Path]::GetFullPath($BackupDirectory)
if ($backupRoot.TrimEnd('\','/') -eq [IO.Path]::GetPathRoot($backupRoot).TrimEnd('\','/')) {
    throw 'A drive root cannot be used as the backup directory'
}
$backupPrefix = $backupRoot.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
if ((Get-Item -LiteralPath $backupRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'A linked backup directory is not supported' }
function Get-CheckedBackupPath([string]$Name) {
    if ([IO.Path]::GetFileName($Name) -ne $Name) { throw 'Invalid backup filename' }
    $resolved = [IO.Path]::GetFullPath((Join-Path $backupRoot $Name))
    if (-not $resolved.StartsWith($backupPrefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Backup path escaped the designated directory' }
    return $resolved
}
function Invoke-CheckedDocker([string]$Step,[string[]]$DockerArguments) {
    # Windows PowerShell 5.1 turns every native stderr line into an ErrorRecord,
    # and under 'Stop' the first one aborted the script: compose's "attribute
    # `version` is obsolete" warning killed the first real run after pg_dump had
    # succeeded. A step is judged by its exit code only; stderr stays as text.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $output = @(& docker @DockerArguments 2>&1 | ForEach-Object { "$_" }) }
    finally { $ErrorActionPreference = $previousPreference }
    if ($LASTEXITCODE -ne 0) { throw "Backup step failed: $Step (exit $LASTEXITCODE)" }
    return $output
}
function Get-BackupHash([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $inputStream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($algorithm.ComputeHash($inputStream)).Replace('-','') }
    finally { $inputStream.Dispose(); $algorithm.Dispose() }
}
function Write-AssetArchive([string]$DirectoryName,[string]$Destination) {
    $source = [IO.Path]::GetFullPath((Join-Path $repo $DirectoryName))
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw "Permanent asset directory missing: $DirectoryName" }
    # Check each directory before descending; ZipFile includes hidden files.
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($source)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $item = Get-Item -LiteralPath $directory -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked assets cannot be included in a backup' }
        foreach ($entry in Get-ChildItem -LiteralPath $directory -Force) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked assets cannot be included in a backup' }
            if ($entry.PSIsContainer) { $pending.Push($entry.FullName) }
        }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($source,$Destination)
}
$stamp = (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + ([Guid]::NewGuid().ToString('N').Substring(0,8))
$dumpName = "testova_$stamp.dump"
$storageName = "storage_$stamp.zip"
$imagesName = "images_$stamp.zip"
$manifestName = "manifest_$stamp.json"
$dumpPath = Get-CheckedBackupPath $dumpName
$dumpPartial = Get-CheckedBackupPath "$dumpName.partial"
$storagePath = Get-CheckedBackupPath $storageName
$imagesPath = Get-CheckedBackupPath $imagesName
$manifestPath = Get-CheckedBackupPath $manifestName
$manifestPartial = Get-CheckedBackupPath "$manifestName.partial"
$containerDump = "/tmp/$dumpName"
$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $repo
    $null = Invoke-CheckedDocker 'database dump' @('compose','exec','-T','postgres','pg_dump','-U','testova','-d','testova_db','-Fc','-f',$containerDump)
    $null = Invoke-CheckedDocker 'copy dump' @('compose','cp',"postgres:$containerDump",$dumpPartial)
    if (-not (Test-Path -LiteralPath $dumpPartial -PathType Leaf) -or (Get-Item -LiteralPath $dumpPartial).Length -lt 64) { throw 'Database dump is missing or incomplete' }
    $stream = [IO.File]::OpenRead($dumpPartial)
    try {
        $header = New-Object byte[] 5
        $null = $stream.Read($header,0,5)
        if ([Text.Encoding]::ASCII.GetString($header) -ne 'PGDMP') { throw 'Invalid dump signature' }
    } finally { $stream.Dispose() }
    $toc = Invoke-CheckedDocker 'dump table-of-contents validation' @('compose','exec','-T','postgres','pg_restore','--list',$containerDump)
    if (-not ($toc -match 'TABLE DATA.*public users')) { throw 'Dump contains no users table data' }
    Write-AssetArchive 'storage' $storagePath
    Write-AssetArchive 'temp_images' $imagesPath
    Move-Item -LiteralPath $dumpPartial -Destination $dumpPath
    $manifest = [ordered]@{
        complete = $true
        created_utc = (Get-Date).ToUniversalTime().ToString('o')
        dump = $dumpName; storage = $storageName; images = $imagesName
        dump_sha256 = Get-BackupHash $dumpPath
        storage_sha256 = Get-BackupHash $storagePath
        images_sha256 = Get-BackupHash $imagesPath
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPartial -Encoding UTF8
    Move-Item -LiteralPath $manifestPartial -Destination $manifestPath
    # Resolve and verify every exact retention target. No recursion or wildcard delete.
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    foreach ($record in Get-ChildItem -LiteralPath $backupRoot -Filter 'manifest_*.json' -File) {
        if ($record.LastWriteTime -ge $cutoff -or $record.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        if ($record.Name -notmatch '^manifest_(\d{8}_\d{6}_[0-9a-f]{8})\.json$') { continue }
        $oldStamp = $Matches[1]
        try { $old = Get-Content -LiteralPath $record.FullName -Raw | ConvertFrom-Json } catch { continue }
        if ($old.complete -isnot [bool] -or -not $old.complete) { continue }
        if ($old.dump -ne "testova_$oldStamp.dump" -or $old.storage -ne "storage_$oldStamp.zip" -or $old.images -ne "images_$oldStamp.zip") { continue }
        $targets = @((Get-CheckedBackupPath $old.dump),(Get-CheckedBackupPath $old.storage),(Get-CheckedBackupPath $old.images),(Get-CheckedBackupPath $record.Name))
        $valid = $true
        foreach ($target in $targets) {
            if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { $valid = $false; break }
        }
        if ($valid) { foreach ($target in $targets) { Remove-Item -LiteralPath $target -Force } }
    }
    Write-Host "Backup tayyor: $dumpPath"
} finally { Set-Location -LiteralPath $previousLocation.Path }
