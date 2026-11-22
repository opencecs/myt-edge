param([Parameter(Mandatory=$true)][string]$Output, [string]$Source = '', [string]$PowerShell = 'powershell.exe')
$ErrorActionPreference = 'Stop'
if (-not $Source) { $Source = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'flash' }
if (Test-Path -LiteralPath $Output) { throw 'A fresh test output directory is required' }
[void](New-Item -ItemType Directory -Path $Output)
$Output = [IO.Path]::GetFullPath($Output)
$code = @'
using System;
using System.IO;
class FakeFastboot {
  static int Main(string[] args) {
    File.AppendAllText(Environment.GetEnvironmentVariable("MOCK_TRACE"), String.Join(" ", args) + "\n");
    string mode=Environment.GetEnvironmentVariable("MOCK_MODE");
    ulong sector=UInt64.Parse(Environment.GetEnvironmentVariable("MOCK_SECTOR"));
    ulong cap=(mode=="small-disk"?32UL:64UL)*1024*1024*1024;
    ulong boot=512UL*1048576, slot=16UL*1024*1024*1024;
    string statePath=Environment.GetEnvironmentVariable("MOCK_TRACE")+".state";
    string state=File.Exists(statePath)?File.ReadAllText(statePath):"idle";
    int at=Array.IndexOf(args,"getvar");
    if(at>=0) {
      string name=args[at+1],value;
      if(name=="serialno") value=mode=="serial"?"WRONG":(mode=="serial-prefix"?"0xTEST-ONLY":"TEST-ONLY");
      else if(name=="debian-board") value=mode=="board-mismatch"?"Y4":"Y12";
      else if(name=="debian-flash-version") value=mode=="legacy"?"0":(mode=="protocol1"?"1":"3");
      else if(name=="debian-disk-info") value="block-size="+sector+",lastLBA="+(cap/sector-1)+",bytes="+cap;
      else if(name=="debian-gpt-ready") value=state=="gpt"&&mode!="gpt-no"?"yes":"no";
      else if(name=="partition-size:boot" || name=="partition-size:slot-a" || name=="partition-size:slot-b" || name=="partition-size:data") {
        ulong size=name=="partition-size:boot"?boot:(name=="partition-size:data"?
          ((cap/sector-1-16384/sector)*sector/1048576)*1048576-10UL*1048576-boot-2*slot:slot);
        if(mode=="stale-"+name.Substring("partition-size:".Length)) size-=1048576;
        value="0x"+size.ToString("x");
      } else return 2;
      Console.Error.WriteLine("(bootloader) "+name+": "+value);
      return 0;
    }
    int flashAt=Array.IndexOf(args,"flash");
    if(flashAt>=0) {
      string part=args[flashAt+1];
      if(part!="gpt" && part!="boot" && part!="slot-a" && part!="slot-b") return 7;
      if(!File.Exists(args[flashAt+2])) return 9;
      if(part=="gpt") {
        if(mode=="live") { Console.Error.WriteLine("(bootloader) Writing GPT 0%"); Console.Error.Flush(); System.Threading.Thread.Sleep(2000); }
        File.WriteAllText(statePath,"gpt");
      }
      else if(state!="gpt") return 4;
      if(mode==part+"-failed0") { Console.Error.WriteLine("FAILED (remote: write)"); return 0; }
      Console.Error.WriteLine("OKAY"); return 0;
    }
    return 2;
  }
}
'@
$cs = Join-Path $Output 'FakeFastboot.cs'
$exe = Join-Path $Output 'FakeFastboot.exe'
[IO.File]::WriteAllText($cs, $code)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:exe ("/out:" + $exe) $cs
if ($LASTEXITCODE -ne 0) { throw 'Cannot compile the test-only Fastboot process' }
$cases = @(
    @{sector=512; mode='ok'; constraint=0; success=$true; flashes='gpt,boot,slot-a,slot-b'},
    @{sector=4096; mode='ok'; constraint=0; success=$true; flashes='gpt,boot,slot-a,slot-b'},
    @{sector=512; mode='serial-prefix'; constraint=0; success=$true; flashes='gpt,boot,slot-a,slot-b'},
    @{sector=4096; mode='ok'; constraint=512; success=$false; flashes=''},
    @{sector=512; mode='legacy'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='protocol1'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='board-y12'; constraint=0; success=$true; flashes='gpt,boot,slot-a,slot-b'},
    @{sector=512; mode='board-mismatch'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='serial'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='stale-boot'; constraint=0; success=$false; flashes='gpt'},
    @{sector=512; mode='stale-slot-a'; constraint=0; success=$false; flashes='gpt'},
    @{sector=512; mode='stale-slot-b'; constraint=0; success=$false; flashes='gpt'},
    @{sector=512; mode='stale-data'; constraint=0; success=$false; flashes='gpt'},
    @{sector=512; mode='small-disk'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='large-image'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='tamper'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='missing-hash'; constraint=0; success=$false; flashes=''},
    @{sector=512; mode='gpt-failed0'; constraint=0; success=$false; flashes='gpt'},
    @{sector=512; mode='gpt-no'; constraint=0; success=$false; flashes='gpt'},
    @{sector=512; mode='boot-failed0'; constraint=0; success=$false; flashes='gpt,boot'},
    @{sector=512; mode='slot-a-failed0'; constraint=0; success=$false; flashes='gpt,boot,slot-a'},
    @{sector=512; mode='slot-b-failed0'; constraint=0; success=$false; flashes='gpt,boot,slot-a,slot-b'},
    @{sector=512; mode='live'; constraint=0; success=$true; flashes='gpt,boot,slot-a,slot-b'},
    @{sector=512; mode='verify'; constraint=0; success=$true; flashes=''},
    @{sector=512; mode='cmd-verify'; constraint=0; success=$true; flashes=''},
    @{sector=512; mode='no-authorization'; constraint=0; success=$false; flashes=''}
)
$results = @()
foreach ($case in $cases) {
    $directory = Join-Path $Output ($case.sector.ToString() + '-' + $case.mode + '-' + $case.constraint)
    [void](New-Item -ItemType Directory -Path (Join-Path $directory 'tools'))
    [void](New-Item -ItemType Directory -Path (Join-Path $directory 'images'))
    Copy-Item -LiteralPath $exe -Destination (Join-Path $directory 'tools/fastboot.exe')
    foreach ($name in @('Flash.cmd','Flash-Debian.ps1','Flash-Contract.ps1','Flash.sh','flash_contract.py')) {
        $targetName = if ($name -in @('Flash.cmd','Flash.sh')) { $name } else { 'tools/' + $name }
        Copy-Item -LiteralPath (Join-Path $Source $name) -Destination (Join-Path $directory $targetName)
    }
    $stream = [IO.File]::Open((Join-Path $directory 'images/rootfs.img'), [IO.FileMode]::CreateNew)
    $writer = New-Object IO.BinaryWriter($stream)
    $writer.Write([uint32]3978755898); $writer.Write([uint16]1); $writer.Write([uint16]0)
    $writer.Write([uint16]28); $writer.Write([uint16]12); $writer.Write([uint32]1048576)
    $imageBlocks = if ($case.mode -eq 'large-image') { 16385 } else { 64 }
    $writer.Write([uint32]$imageBlocks); $writer.Write([uint32]1); $writer.Write([uint32]0)
    $writer.Dispose(); $stream.Dispose()
    foreach ($name in @('images/gpt-512.img','images/gpt-4096.img','images/boot-512.img','images/boot-4096.img','images/uefi.bin','images/ddr.bin')) {
        [IO.File]::WriteAllText((Join-Path $directory $name), 'fixture-' + $name)
    }
    $inventory = @{}
    Get-ChildItem -LiteralPath $directory -Recurse -File | ForEach-Object {
        $name = $_.FullName.Substring($directory.Length+1).Replace('\','/')
        $inventory[$name] = @{bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
    }
    $package = @{format='y4-flash-v1';files=$inventory}
    if ($case.mode -like 'board-*') { $package.hardware = 'Y12-6187' }
    [IO.File]::WriteAllText((Join-Path $directory 'tools/package.json'), ($package | ConvertTo-Json -Depth 6))
    $lines = @(Get-ChildItem -LiteralPath $directory -Recurse -File | Sort-Object FullName | ForEach-Object {
        (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' +
            $_.FullName.Substring($directory.Length+1).Replace('\','/')
    })
    [IO.File]::WriteAllText((Join-Path $directory 'SHA256SUMS'), (($lines -join "`n") + "`n"))
    if ($case.mode -eq 'tamper') { [IO.File]::WriteAllText((Join-Path $directory 'images/boot-512.img'), 'tampered') }
    if ($case.mode -eq 'missing-hash') {
        $checksum = Join-Path $directory 'SHA256SUMS'
        [IO.File]::WriteAllText($checksum, (((Get-Content -LiteralPath $checksum | Where-Object { -not $_.EndsWith('  images/boot-512.img') }) -join "`n") + "`n"))
    }
    $trace = Join-Path $directory 'trace.log'
    $env:MOCK_TRACE = $trace; $env:MOCK_MODE = $case.mode; $env:MOCK_SECTOR = $case.sector.ToString()
    $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $directory 'tools/Flash-Debian.ps1'))
    if ($case.mode -eq 'verify') { $arguments += '-VerifyOnly' }
    else {
        if ($case.mode -ne 'no-authorization') { $arguments += @('-SerialNumber','TEST-ONLY') }
        if ($case.constraint) { $arguments += @('-SectorBytes', $case.constraint.ToString()) }
    }
    if ($case.mode -eq 'cmd-verify') {
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $result = & (Join-Path $directory 'Flash.cmd') -VerifyOnly 2>&1; $rc = $LASTEXITCODE }
        finally { $ErrorActionPreference = $old }
        $text = ($result | ForEach-Object { $_.ToString() }) -join "`n"
    } elseif ($case.mode -eq 'live') {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $PowerShell
        $info.Arguments = ($arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
        $info.UseShellExecute = $false; $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $info; [void]$process.Start()
        $text = ''; $seenLive = $false
        while (($line = $process.StandardOutput.ReadLine()) -ne $null) {
            $text += $line + "`n"
            if ($line -match 'Writing GPT 0%') {
                if ($process.HasExited -or (Test-Path -LiteralPath ($trace+'.state'))) { throw 'Windows native progress was buffered until completion' }
                $gptLogs = @(Get-ChildItem -LiteralPath (Join-Path $directory 'logs') -Recurse -Filter 'gpt.log')
                if ($gptLogs.Count -ne 1 -or (Get-Content -LiteralPath $gptLogs[0].FullName -Raw) -notmatch '0%') { throw 'Live progress was not flushed to disk' }
                $seenLive = $true
            }
        }
        $text += $process.StandardError.ReadToEnd(); $process.WaitForExit(); $rc = $process.ExitCode
        $process.Dispose()
        if (-not $seenLive) { throw 'Missing live Windows progress' }
    } else {
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $result = & $PowerShell @arguments 2>&1; $rc = $LASTEXITCODE }
        finally { $ErrorActionPreference = $old }
        $text = ($result | ForEach-Object { $_.ToString() }) -join "`n"
    }
    [IO.File]::WriteAllText((Join-Path $directory 'result.log'), $text)
    if (($rc -eq 0) -ne $case.success) { throw "Unexpected Windows flash result: $directory`n$text" }
    $calls = @()
    if (Test-Path -LiteralPath $trace) { $calls = @(Get-Content -LiteralPath $trace) }
    $flashes = @($calls | ForEach-Object { if ($_ -match ' flash ([a-z-]+) ') { $Matches[1] } }) -join ','
    if ($flashes -ne $case.flashes) { throw "Unexpected device writes: $directory`n$($calls -join '`n')" }
    if ($case.success -and $case.mode -in @('ok','live','serial-prefix')) {
        $gptAt = -1
        for ($index=0; $index -lt $calls.Count; $index++) { if ($calls[$index] -match ' flash gpt ') { $gptAt=$index; break } }
        foreach ($name in @('serialno','debian-flash-version','debian-disk-info')) {
            $before = @($calls[0..($gptAt-1)] | Where-Object { $_.EndsWith('getvar '+$name) })
            if ($before.Count -ne 1) { throw 'Flash ran before complete target/geometry verification' }
        }
        $gates = @('debian-gpt-ready','partition-size:boot','partition-size:slot-a','partition-size:slot-b','partition-size:data')
        for ($index=0; $index -lt $gates.Count; $index++) {
            if (-not $calls[$gptAt+1+$index].EndsWith('getvar ' + $gates[$index])) { throw 'Protocol3 gate order is invalid' }
        }
        if (@($calls | Where-Object { $_ -match ' erase | flash (root|data) ' }).Count) { throw 'Unexpected destructive command' }
        $gpt = if ($case.sector -eq 512) { 'images/gpt-512.img' } else { 'images/gpt-4096.img' }
        if (-not ($calls | Where-Object { $_.EndsWith('flash gpt ' + $gpt) })) { throw 'Wrong sector template selected' }
        $bootImage = if ($case.sector -eq 512) { 'images/boot-512.img' } else { 'images/boot-4096.img' }
        if (-not ($calls | Where-Object { $_.EndsWith('flash boot ' + $bootImage) })) { throw 'Wrong sector boot image selected' }
    }
    $results += @{sector=$case.sector; mode=$case.mode; exit=$rc; expected_success=$case.success; flashes=$flashes}
}
Remove-Item Env:MOCK_TRACE,Env:MOCK_MODE,Env:MOCK_SECTOR
[IO.File]::WriteAllText((Join-Path $Output 'VERIFICATION.json'), (@{complete=$true; device_access=$false; cases=$results; live_output_verified=$true} | ConvertTo-Json -Depth 6))
Write-Host 'PASS: Windows protocol3 A/B flash flow, auto512/4Kn, serial/hash/layout/FAILED rc0 failures, live progress; no hardware'
