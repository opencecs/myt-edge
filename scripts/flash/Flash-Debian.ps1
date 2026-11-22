param([string]$SerialNumber, [switch]$VerifyOnly, [ValidateNotNullOrEmpty()][string]$ExtractSpiBootstrap,
      [ValidateSet(0,512,4096)][int]$SectorBytes = 0)
$ErrorActionPreference = 'Stop'
$packageDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $packageDirectory
$checksums = @{}
foreach ($line in Get-Content -LiteralPath 'SHA256SUMS') {
    if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw 'Invalid checksum line' }
    $expected = $Matches[1]; $name = $Matches[2]
    if ($checksums.ContainsKey($name) -or $name -match '\\|:|(^|/)\.{1,2}(/|$)|//|^/') { throw 'Unsafe or duplicate package path' }
    $target = [IO.Path]::GetFullPath((Join-Path $packageDirectory $name))
    if (-not $target.StartsWith($packageDirectory + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe package path' }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw "Hash mismatch: $name" }
    $checksums[$name] = $expected
}
$manifest = Get-Content -LiteralPath 'tools/package.json' -Raw | ConvertFrom-Json
if ($manifest.format -cnotin @('y4-flash-v1','y4-flash-v2')) { throw 'Unsupported flash package format' }
$manifestFiles = @($manifest.files.PSObject.Properties)
if ($checksums.Count -ne $manifestFiles.Count + 1 -or -not $checksums.ContainsKey('tools/package.json')) { throw 'Incomplete package checksum inventory' }
foreach ($entry in $manifestFiles) {
    if (-not $checksums.ContainsKey($entry.Name) -or $checksums[$entry.Name] -ne $entry.Value.sha256 -or
        (Get-Item -LiteralPath (Join-Path $packageDirectory $entry.Name)).Length -ne $entry.Value.bytes) { throw "Package manifest mismatch: $($entry.Name)" }
}
$required = @('Flash.cmd','Flash.sh','tools/Flash-Debian.ps1','tools/Flash-Contract.ps1','tools/flash_contract.py','images/rootfs.img',
              'images/gpt-512.img','images/gpt-4096.img','images/boot-512.img','images/boot-4096.img')
if ($manifest.format -ceq 'y4-flash-v2') {
    $required += @('images/myt-spi.bin','tools/spi_bundle.py')
    if ($manifest.board_check -isnot [bool] -or !$manifest.board_check -or $manifest.hardware -cnotin @('Y4-6187','Y12-6187')) {
        throw 'Single-image package requires a verified board target'
    }
    $binaries = @(Get-ChildItem -LiteralPath 'images' -Force | Where-Object { $_.Extension -eq '.bin' })
    if ($binaries.Count -ne 1 -or $binaries[0].Name -cne 'myt-spi.bin') { throw 'Expected myt-spi.bin as the only SPI image' }
} else {
    $required += @('images/uefi.bin','images/ddr.bin')
    if ($ExtractSpiBootstrap) { throw 'Bootstrap extraction requires a single-image v2 full package' }
}
foreach ($name in $required) {
    if (-not $checksums.ContainsKey($name)) { throw "Missing required package file: $name" }
}
. (Join-Path $PSScriptRoot 'Flash-Contract.ps1')
if ($manifest.format -ceq 'y4-flash-v2') {
    $bootstrap = Get-MytSpiBootstrap 'images/myt-spi.bin' ($manifest.hardware -split '-')[0]
    if ($ExtractSpiBootstrap) {
        if ($SerialNumber -or $VerifyOnly -or $SectorBytes) { throw 'Use -ExtractSpiBootstrap only with its new output file' }
        Export-MytSpiBootstrap $bootstrap $ExtractSpiBootstrap
        exit 0
    }
}
if ($VerifyOnly) { Write-Host 'Package hashes verified; no device accessed.'; exit 0 }
if (-not $SerialNumber) { throw 'Use -SerialNumber SERIAL. This writes the GPT, boot and both root slots; no erase step belongs to flashing.' }
if ($SerialNumber -notmatch '^[A-Za-z0-9._-]+$') { throw 'Invalid target serial' }
$fb = Join-Path $PSScriptRoot 'fastboot.exe'
$log = Join-Path $packageDirectory ('logs/flash-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + $PID)
[void](New-Item -ItemType Directory -Path $log -Force)
function Invoke-Fastboot([string]$Stage, [string[]]$Arguments) {
    $capture = New-Object Text.StringBuilder
    $writer = New-Object IO.StreamWriter((Join-Path $log ($Stage + '.log')), $false, (New-Object Text.UTF8Encoding($false)))
    $writer.AutoFlush = $true
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $fb @Arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            $writer.WriteLine($line)
            [void]$capture.AppendLine($line)
            Write-Host $line
        }
        $rc = $LASTEXITCODE
    } finally { $ErrorActionPreference = $oldPreference; $writer.Dispose() }
    $text = $capture.ToString()
    if ($rc -ne 0 -or $text -match '(?i)\bFAILED\b|fastboot:\s*error:') { throw "Flash stopped at $Stage; original logs: $log`n$text" }
    if ($Arguments -contains 'erase' -or $Arguments -contains 'flash') { Test-Y4Success $text }
    return $text
}
$actualSerial = Invoke-Fastboot -Stage 'serial' -Arguments @('-s', $SerialNumber, 'getvar', 'serialno')
Test-Y4Serial $actualSerial $SerialNumber
$hardware = $manifest.hardware
if ($null -eq $hardware) { $hardware = "Y4-6187" }
if ($hardware -cnotin @("Y4-6187", "Y12-6187")) { throw "Unsupported package target hardware" }
$target = ($hardware -split "-")[0]
if ($manifest.board_check -eq $true -or $target -ceq 'Y12') {
    $board = Invoke-Fastboot -Stage 'board' -Arguments @('-s', $SerialNumber, 'getvar', 'debian-board')
    Test-Y4Ready $board 'debian-board' $target
}
$version = Invoke-Fastboot -Stage 'protocol' -Arguments @('-s', $SerialNumber, 'getvar', 'debian-flash-version')
$disk = Invoke-Fastboot -Stage 'disk' -Arguments @('-s', $SerialNumber, 'getvar', 'debian-disk-info')
$imageBytes = Get-Y4RootImageBytes (Join-Path $packageDirectory 'images/rootfs.img')
$layout = Get-Y4Geometry $version $disk $imageBytes $SectorBytes
[IO.File]::WriteAllText((Join-Path $log 'layout.json'), ($layout | ConvertTo-Json))
if ($layout.sector_bytes -eq 512) { $gpt = 'images/gpt-512.img'; $boot = 'images/boot-512.img' }
else { $gpt = 'images/gpt-4096.img'; $boot = 'images/boot-4096.img' }
Write-Host 'Writing the GPT, boot and both root slots; the data partition is left untouched.'
$null = Invoke-Fastboot -Stage 'gpt' -Arguments @('-s', $SerialNumber, '-S', '64M', 'flash', 'gpt', $gpt)
$gptReady = Invoke-Fastboot -Stage 'gpt-ready' -Arguments @('-s', $SerialNumber, 'getvar', 'debian-gpt-ready')
Test-Y4Ready $gptReady 'debian-gpt-ready' 'yes'
foreach ($partition in @('boot','slot-a','slot-b','data')) {
    $size = Invoke-Fastboot -Stage ($partition + '-size') -Arguments @('-s', $SerialNumber, 'getvar', ('partition-size:' + $partition))
    $key = 'expected_' + $partition.Replace('-', '_') + '_bytes'
    Test-Y4Partition $size $partition $layout.$key
}
$null = Invoke-Fastboot -Stage 'boot' -Arguments @('-s', $SerialNumber, '-S', '64M', 'flash', 'boot', $boot)
foreach ($partition in @('slot-a','slot-b')) {
    $null = Invoke-Fastboot -Stage $partition -Arguments @('-s', $SerialNumber, '-S', '64M', 'flash', $partition, 'images/rootfs.img')
}
foreach ($partition in @('slot-a','slot-b')) {
    $size = Invoke-Fastboot -Stage ($partition + '-size-final') -Arguments @('-s', $SerialNumber, 'getvar', ('partition-size:' + $partition))
    Test-Y4Partition $size $partition $layout.expected_slot_a_bytes
}
Write-Host "Debian written to both slots. First boot provisions data, clears stale signatures, grows the slot filesystem, then starts normal services. Logs: $log"
Write-Host 'For the board SPI update, follow README.txt. Reboot explicitly when ready.'
