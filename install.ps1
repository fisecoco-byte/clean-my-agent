param(
    [string]$Repo = 'fisecoco-byte/clean-my-agent',
    [string]$Version = 'latest',
    [string]$Source = '',
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\clean-my-agent",
    [switch]$NoPath,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Use install.sh on macOS / Linux.' }
if ($Repo -notmatch '^[\w.-]+/[\w.-]+$') { throw 'Repo must be owner/repo.' }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$target = Join-Path $InstallDir 'clean-my-agent.exe'
$marker = Join-Path $InstallDir '.clean-my-agent-install'
$pathMarker = Join-Path $InstallDir '.clean-my-agent-path-added'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($userPath -split ';' | Where-Object { $_ })
if ($Uninstall) {
    if (!(Test-Path $marker) -or (Get-Content $marker -Raw).Trim() -ne $target) { throw 'No managed installation found.' }
    Remove-Item -LiteralPath $target, $marker -Force
    if (Test-Path $pathMarker) {
        $entries = @($entries | Where-Object { $_.TrimEnd('\') -ine $InstallDir.TrimEnd('\') })
        [Environment]::SetEnvironmentVariable('Path', ($entries -join ';'), 'User')
        $env:Path = (($env:Path -split ';' | Where-Object { $_.TrimEnd('\') -ine $InstallDir.TrimEnd('\') }) -join ';')
        Remove-Item -LiteralPath $pathMarker -Force
    }
    Write-Host 'Uninstalled. Reports, policy and staged data are retained.'
    return
}
$arch = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
if ($arch -eq 'ARM64') { throw 'This release supports Windows x64. ARM64 is not yet packaged.' }
if ($arch -ne 'AMD64') { throw 'Windows x64 is required.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('clean-my-agent-' + [Guid]::NewGuid().ToString('N'))
New-Item $scratch -ItemType Directory | Out-Null
$pending = $null
try {
    if (!$Source -and $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'BUNDLE_TARGET'))) {
        if ((Get-Content (Join-Path $PSScriptRoot 'BUNDLE_TARGET') -Raw).Trim() -ne 'windows-x64') { throw 'Wrong platform package.' }
        $Source = Join-Path $PSScriptRoot 'clean-my-agent.exe'
        $Version = (Get-Content (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim()
    }
    $candidate = Join-Path $scratch 'clean-my-agent.exe'
    if ($Source) {
        Copy-Item -LiteralPath $Source -Destination $candidate
    } else {
        if ($Version -eq 'latest') {
            $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
            $Version = $release.tag_name
        }
        if ($Version -notmatch '^v\d+\.\d+\.\d+(-[\w.-]+)?$') { throw 'A published vX.Y.Z release is required.' }
        $asset = "clean-my-agent-$Version-windows-x64.zip"
        $base = "https://github.com/$Repo/releases/download/$Version"
        Write-Host "Downloading $asset..."
        Invoke-WebRequest "$base/$asset" -OutFile (Join-Path $scratch 'package.zip') -UseBasicParsing
        Invoke-WebRequest "$base/SHA256SUMS" -OutFile (Join-Path $scratch 'SHA256SUMS') -UseBasicParsing
        $checksums = [IO.File]::ReadAllText((Join-Path $scratch 'SHA256SUMS'))
        $matches = @($checksums -split "`n" | Where-Object { $_ -match ('^[a-fA-F0-9]{64}\s+' + [regex]::Escape($asset) + '\s*$') })
        if ($matches.Count -ne 1) { throw 'Missing or duplicate checksum.' }
        $expected = ($matches[0] -split '\s+')[0]
        if ((Get-FileHash (Join-Path $scratch 'package.zip') -Algorithm SHA256).Hash -ine $expected) { throw 'SHA256 mismatch. Existing installation retained.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead((Join-Path $scratch 'package.zip'))
        try {
            $entry = $zip.GetEntry('clean-my-agent/clean-my-agent.exe')
            if (!$entry) { throw 'Executable missing from archive.' }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $candidate)
        } finally { $zip.Dispose() }
    }
    $probe = & $candidate --version
    if ($LASTEXITCODE -ne 0 -or $probe -notmatch '^clean-my-agent \d+\.\d+\.\d+') { throw 'Executable verification failed.' }
    if ($Version -ne 'latest' -and $probe -ne "clean-my-agent $($Version.TrimStart('v'))") { throw 'Version mismatch.' }
    New-Item $InstallDir -ItemType Directory -Force | Out-Null
    if (Test-Path $target) {
        if (!(Test-Path $marker) -or (Get-Content $marker -Raw).Trim() -ne $target) { throw 'An unmanaged command already exists; it was not overwritten.' }
    }
    $pending = Join-Path $InstallDir ('.install-' + [Guid]::NewGuid().ToString('N') + '.exe')
    Copy-Item $candidate $pending
    if (Test-Path $target) { [IO.File]::Replace($pending, $target, $null) } else { [IO.File]::Move($pending, $target) }
    Set-Content $marker $target -Encoding UTF8
    if (!$NoPath) {
        if (!($entries | Where-Object { $_.TrimEnd('\') -ieq $InstallDir.TrimEnd('\') })) {
            [Environment]::SetEnvironmentVariable('Path', (($entries + $InstallDir) -join ';'), 'User')
            Set-Content $pathMarker 'managed PATH entry' -Encoding UTF8
        }
        if (!(($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $InstallDir.TrimEnd('\') })) { $env:Path = "$InstallDir;$env:Path" }
    }
    Write-Host "Installed $probe. Run: clean-my-agent"
} finally {
    if ($pending -and (Test-Path $pending)) { Remove-Item -LiteralPath $pending -Force }
    Remove-Item -LiteralPath $scratch -Recurse -Force
}
