# Shared validation for Windows PowerShell 5.1 and PowerShell 7.
function Get-MytSpiBootstrap {
    param([string]$Bundle, [ValidateSet('Y4','Y12')][string]$Board)
    # MYTSPI01 wire format; keep the three vendor container checks aligned with spi_bundle.py.
    $file = Get-Item -LiteralPath $Bundle -ErrorAction Stop
    $payloadStart = 156   # CIX header (32), board/version (16), three length + SHA-256 descriptors
    $maxSize = $payloadStart + 32 + 3 * 32 + 0x4000 + 0x1000 + 0x202000
    if ($file.Length -lt $payloadStart + 32 -or $file.Length -gt $maxSize) { throw 'Invalid SPI bundle size' }
    $data = [IO.File]::ReadAllBytes($file.FullName)
    $end = $data.Length - 32
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        function Test-Hash([int]$Offset, [int]$Count, [int]$DigestAt) {
            $actual = [BitConverter]::ToString($hash.ComputeHash($data, $Offset, $Count))
            if ($actual -cne [BitConverter]::ToString($data, $DigestAt, 32)) { throw 'SPI bundle hash mismatch' }
        }
        function Test-Words([int]$Offset, [long[]]$Expected) {
            foreach ($word in $Expected) {
                if ([BitConverter]::ToUInt32($data, $Offset) -ne $word) { throw 'Unexpected SPI container header or length' }
                $Offset += 4
            }
        }
        Test-Hash 0 $end $end
        Test-Words 0 @(0x55AA55AA, 1, 1, 0x80000000L, 0xFE, 0, ($data.Length - 32), 0)
        if ([Text.Encoding]::ASCII.GetString($data, 32, 8) -cne 'MYTSPI01') { throw 'Invalid SPI bundle signature' }
        $boardId = if ($Board -ceq 'Y4') { 4 } else { 12 }
        Test-Words 40 @($boardId, 1)
        $parts = @(
            @{kind=3; base=0x3F8000; limit=0x4000; flags=0x80000008L},
            @{kind=4; base=0x3FC000; limit=0x1000; flags=0x80000010L},
            @{kind=7; base=0x3FE000; limit=0x202000; flags=0x80000000L}
        )
        $cursor = $payloadStart
        $entry = 48
        foreach ($part in $parts) {
            $length = [BitConverter]::ToUInt32($data, $entry)
            if ($length -lt 32 -or $length -gt ($end - $cursor)) { throw 'Truncated SPI container' }
            $rawLength = [BitConverter]::ToUInt32($data, $cursor + 24)
            if ($rawLength -lt 1 -or $rawLength -gt $part.limit -or ($part.kind -eq 4 -and $rawLength -ne $part.limit)) {
                throw 'Invalid vendor payload size'
            }
            Test-Words $cursor @(0x55AA55AA, 1, 1, $part.flags, $part.kind, $part.base, $rawLength, 0)
            if ($length -ne [Math]::Max(4112, 32 + [int]$rawLength)) { throw 'Invalid vendor container length' }
            for ($index = $cursor + 32 + $rawLength; $index -lt $cursor + $length; $index++) {
                if ($data[$index] -ne 0) { throw 'Invalid vendor container padding' }
            }
            Test-Hash $cursor $length ($entry + 4)
            if ($part.kind -eq 7) {
                $bl33 = [pscustomobject]@{ Data=$data; Offset=$cursor; Length=$length }
            }
            $cursor += $length
            $entry += 36
        }
        if ($cursor -ne $end) { throw 'Unexpected data after last SPI container' }
        return $bl33
    } finally { $hash.Dispose() }
}

function Export-MytSpiBootstrap($VerifiedPart, [string]$OutputFile) {
    $path = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFile)
    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($VerifiedPart.Data, $VerifiedPart.Offset, $VerifiedPart.Length) } finally { $stream.Dispose() }
    Write-Host ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $path)
}

function Get-Y4Variable([string]$Output, [string]$Name) {
    if ($Output -match '(?i)\bFAILED\b|fastboot:\s*error:') { throw 'Fastboot reported failure; no later operation is permitted' }
    $pattern = '(?m)^\s*(?:\(bootloader\)\s*)?' + [regex]::Escape($Name) + ':\s*(.*?)\s*$'
    $values = @([regex]::Matches($Output, $pattern) | ForEach-Object { $_.Groups[1].Value.Trim() } | Select-Object -Unique)
    if ($values.Count -ne 1 -or -not $values[0]) { throw "Missing or ambiguous Fastboot variable: $Name" }
    return [string]$values[0]
}

function Test-Y4Serial([string]$Output, [string]$Expected) {
    $reported = Get-Y4Variable $Output 'serialno'
    # The UEFI getvar response can prefix the enumerated USB serial with 0x.
    if ($Expected -notmatch '^[A-Za-z0-9._-]+$' -or
        (($reported -creplace '^0x', '') -cne ($Expected -creplace '^0x', ''))) {
        throw 'Connected Fastboot serial does not match the requested target'
    }
}

function Test-Y4Ready([string]$Output, [string]$Name, [string]$Expected) {
    if ((Get-Y4Variable $Output $Name) -cne $Expected) { throw "UEFI did not confirm $Name=$Expected" }
}

function Test-Y4Success([string]$Output) {
    if ($Output -match '(?i)\bFAILED\b|fastboot:\s*error:' -or $Output -notmatch '\bOKAY\b') {
        throw 'Fastboot did not confirm successful completion'
    }
}

function Get-Y4RootImageBytes([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $reader = New-Object IO.BinaryReader($stream)
    try {
        if ($reader.ReadUInt32() -ne 3978755898L -or $reader.ReadUInt16() -ne 1 -or
            $reader.ReadUInt16() -ne 0 -or $reader.ReadUInt16() -ne 28 -or $reader.ReadUInt16() -ne 12) {
            throw 'Unsupported root sparse image header'
        }
        $block = $reader.ReadUInt32(); $count = $reader.ReadUInt32(); $chunks = $reader.ReadUInt32()
        if (-not $block -or $block % 4096 -or $block -gt 1048576 -or -not $count -or -not $chunks) {
            throw 'Invalid root sparse geometry'
        }
        $size = [decimal]$block * [decimal]$count
        if ($size % 1048576 -or $size -gt [decimal][UInt64]::MaxValue) { throw 'Invalid root image size' }
        return $size
    } finally { $reader.Dispose(); $stream.Dispose() }
}

function Get-Y4Geometry([string]$VersionOutput, [string]$DiskOutput, [decimal]$ImageBytes, [int]$ExpectedSector = 0) {
    if ((Get-Y4Variable $VersionOutput 'debian-flash-version') -ne '3') {
        throw 'This package requires its source-built Debian UEFI flash protocol 3; update UEFI first'
    }
    $info = Get-Y4Variable $DiskOutput 'debian-disk-info'
    $match = [regex]::Match($info, '^block-size=(512|4096),lastLBA=([0-9]+),bytes=([0-9]+)$')
    if (-not $match.Success) { throw 'Invalid UEFI disk geometry response' }
    $sector = [int]$match.Groups[1].Value
    $bootBytes = 512L * 1048576
    $slotBytes = 16L * 1073741824
    $slotAStart = 10L * 1048576 + $bootBytes
    $dataStart = $slotAStart + 2 * $slotBytes
    $last = [decimal]::Parse($match.Groups[2].Value, [Globalization.CultureInfo]::InvariantCulture)
    $capacity = [decimal]::Parse($match.Groups[3].Value, [Globalization.CultureInfo]::InvariantCulture)
    if ($last -ge [decimal][UInt64]::MaxValue -or $capacity -gt [decimal][UInt64]::MaxValue -or
        $capacity -ne ($last + 1) * $sector -or ($ExpectedSector -ne 0 -and $ExpectedSector -ne $sector)) {
        throw 'UEFI disk capacity or requested sector size is inconsistent'
    }
    if ($ImageBytes -gt $slotBytes) { throw 'Slot image does not fit the fixed 16 GiB slot partition' }
    if ($capacity -lt $dataStart + 1048576) { throw 'System NVMe is smaller than the complete Debian layout' }
    $usable = $last - 16384/$sector - 1
    $dataBytes = [Math]::Floor([decimal](($usable+1)*$sector/1048576))*1048576 - $dataStart
    if ($dataBytes -lt 1048576) { throw 'System NVMe leaves no usable data partition' }
    return [pscustomobject]@{ protocol=3; sector_bytes=$sector; disk_bytes=$capacity; last_lba=$last;
        slot_image_bytes=$ImageBytes; expected_boot_bytes=$bootBytes;
        expected_slot_a_bytes=$slotBytes; expected_slot_b_bytes=$slotBytes;
        expected_data_bytes=$dataBytes; slot_a_start_bytes=$slotAStart; data_start_bytes=$dataStart;
        capacity_source='UEFI system NVMe Block I/O' }
}

function Test-Y4Partition([string]$Output, [string]$Name, [decimal]$Expected) {
    $value = Get-Y4Variable $Output ('partition-size:' + $Name)
    if ($value -notmatch '^(?:0x)?[0-9a-fA-F]+$') { throw "Invalid partition size response: $Name" }
    $actual = [UInt64]::Parse(($value -replace '^0x', ''), [Globalization.NumberStyles]::AllowHexSpecifier,
                            [Globalization.CultureInfo]::InvariantCulture)
    if ([decimal]$actual -ne $Expected) { throw "UEFI partition readback does not match planned $Name size" }
}
