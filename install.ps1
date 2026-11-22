# Splitch Universal Installer & Updater for Windows (PowerShell)
#
# Install or update in one command:
#   irm https://raw.githubusercontent.com/RaveAgainstTheMachine/splitch/main/install.ps1 | iex
#
# Local checkout install:
#   .\install.ps1 -Local
#
# Headless GPU agent install (skips Node.js & UI build):
#   .\install.ps1 -AgentOnly

[CmdletBinding()]
param(
    [switch]$Local,
    [switch]$AgentOnly,
    [switch]$NoUiBuild,
    [switch]$Uninstall,
    [switch]$Purge,
    [string]$Dir = "",
    [string]$Repo = "",
    [string]$Branch = "",
    [string]$Python = "3.12",
    [string]$Engine = "",
    [string]$Backend = "",
    [switch]$WithMedia,
    [string]$Join = "",
    [string]$JoinToken = "",
    [string]$NodeId = "",
    [string]$NodeName = "",
    [string]$Role = "",
    [switch]$YieldOnGaming,
    [string]$YieldGpus = "",
    [switch]$EnableTools,
    [switch]$NoDesktop
)

$ErrorActionPreference = "Stop"

$DataRoot = if ($env:SPLITCH_DATA_ROOT) { $env:SPLITCH_DATA_ROOT } else { Join-Path $env:LOCALAPPDATA "splitch" }
$AppDir = if ($Dir) { $Dir } elseif ($env:SPLITCH_HOME) { $env:SPLITCH_HOME } else { Join-Path $DataRoot "app" }
$BinDir = Join-Path $DataRoot "bin"
$RepoUrl = if ($Repo) { $Repo } elseif ($env:SPLITCH_REPO) { $env:SPLITCH_REPO } else { "https://github.com/RaveAgainstTheMachine/splitch.git" }
$TargetBranch = if ($Branch) { $Branch } elseif ($env:SPLITCH_BRANCH) { $env:SPLITCH_BRANCH } else { "main" }
$TargetEngine = if ($Engine) { $Engine } elseif ($env:SPLITCH_ENGINE) { $env:SPLITCH_ENGINE } else { "auto" }
$TargetBackend = if ($Backend) { $Backend } elseif ($env:SPLITCH_ENGINE_BACKEND) { $env:SPLITCH_ENGINE_BACKEND } else { "auto" }
$UvVersion = if ($env:SPLITCH_UV_VERSION) { $env:SPLITCH_UV_VERSION } else { "0.13.5" }
$NodeVersion = if ($env:SPLITCH_NODE_VERSION) { $env:SPLITCH_NODE_VERSION } else { "22.14.0" }
if ($env:SPLITCH_AGENT_ONLY -eq "1") { $AgentOnly = $true }
if ($env:SPLITCH_NO_UI_BUILD -eq "1") { $NoUiBuild = $true }
if ($env:SPLITCH_WITH_MEDIA -eq "1") { $WithMedia = $true }

$Esc = [char]27
$IsTty = -not [Console]::IsOutputRedirected
function Set-OscProgress([int]$State, [int]$Pct = 0) {
    if ($IsTty) { [Console]::Write("$Esc]9;4;$State;$Pct$Esc\") }
}
function Show-StepBar([int]$Cur, [int]$Total, [string]$Label) {
    $Pct = [int](($Cur * 100) / $Total)
    $Width = 20
    $Fill = [int](($Cur * $Width) / $Total)
    $Empty = $Width - $Fill
    $Bar = ([string][char]0x2501 * $Fill) + ([string][char]0x2500 * $Empty)
    Set-OscProgress 1 $Pct
    Write-Progress -Activity "Splitch Installer" -Status "[$Cur/$Total] $Label" -PercentComplete $Pct
    Write-Host ""
    Write-Host ("[{0}/{1}] " -f $Cur, $Total) -ForegroundColor Magenta -NoNewline
    Write-Host ([string][char]0x2501 * $Fill) -ForegroundColor Magenta -NoNewline
    Write-Host ([string][char]0x2500 * $Empty) -ForegroundColor DarkGray -NoNewline
    Write-Host (" {0,3}%  {1}" -f $Pct, $Label)
}
function Download-WithProgress([string]$Url, [string]$OutFile, [string]$Label) {
    try {
        Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
        $Client = [System.Net.Http.HttpClient]::new()
        $Client.Timeout = [TimeSpan]::FromMinutes(10)
        $Resp = $Client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $Resp.EnsureSuccessStatusCode() | Out-Null
        $Total = $Resp.Content.Headers.ContentLength
        $InStream = $Resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $OutStream = [System.IO.File]::Create($OutFile)
        $Buf = New-Object byte[] 65536
        $ReadTotal = [long]0
        $Sw = [System.Diagnostics.Stopwatch]::StartNew()
        $LastDraw = 0.0
        try {
            while (($N = $InStream.Read($Buf, 0, $Buf.Length)) -gt 0) {
                $OutStream.Write($Buf, 0, $N)
                $ReadTotal += $N
                $Now = $Sw.Elapsed.TotalSeconds
                if ($IsTty -and (($Now - $LastDraw) -ge 0.08 -or ($Total -and $ReadTotal -ge $Total))) {
                    $LastDraw = $Now
                    $MbDone = $ReadTotal / 1MB
                    $Speed = if ($Now -gt 0.01) { $MbDone / $Now } else { 0.0 }
                    if ($Total -and $Total -gt 0) {
                        $Pct = [int][Math]::Min(100, ($ReadTotal * 100) / $Total)
                        $MbTot = $Total / 1MB
                        $Fill = [int][Math]::Round(($Pct * 22) / 100)
                        $Bar = ([string][char]0x2501 * $Fill) + ([string][char]0x2500 * (22 - $Fill))
                        Set-OscProgress 1 $Pct
                        Write-Progress -Activity "Downloading $Label" -Status ("{0:N1}/{1:N1} MB ({2:N1} MB/s)" -f $MbDone, $MbTot, $Speed) -PercentComplete $Pct
                        [Console]::Write("`r$Esc[2K  $Label  $Bar {0,3}%  {1:N1}/{2:N1} MB  {3:N1} MB/s" -f $Pct, $MbDone, $MbTot, $Speed)
                    } else {
                        Set-OscProgress 3 0
                        [Console]::Write("`r$Esc[2K  $Label  {0:N1} MB  {1:N1} MB/s" -f $MbDone, $Speed)
                    }
                }
            }
            if ($IsTty) { [Console]::WriteLine("") }
        } finally {
            $OutStream.Dispose()
            $InStream.Dispose()
            $Resp.Dispose()
            $Client.Dispose()
        }
    } catch {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
}

function Write-Step($Msg) { Write-Host "[splitch] $Msg" -ForegroundColor Magenta }
function Write-Ok($Msg)   { Write-Host "  [ok] $Msg" -ForegroundColor Green }
function Write-Warn($Msg) { Write-Host "  [!] $Msg" -ForegroundColor Yellow }

if ($Uninstall) {
    Write-Step "Uninstalling Splitch..."
    $Shim = Join-Path $BinDir "splitch.cmd"
    if (Test-Path $Shim) {
        try { & $Shim stop 2>$null | Out-Null } catch {}
        try { & $Shim desktop uninstall 2>$null | Out-Null } catch {}
        Remove-Item -Force $Shim; Write-Ok "Removed $Shim"
    }
    $LlamaShim = Join-Path $BinDir "llama-server.cmd"
    if (Test-Path $LlamaShim) { Remove-Item -Force $LlamaShim; Write-Ok "Removed $LlamaShim" }
    foreach ($S in @("sd-server.cmd", "sd-cli.cmd")) {
        $P = Join-Path $BinDir $S
        if (Test-Path $P) { Remove-Item -Force $P; Write-Ok "Removed $P" }
    }
    $SdMeta = Join-Path $DataRoot "sd_engine_meta.json"
    if (Test-Path $SdMeta) { Remove-Item -Force $SdMeta }
    $ManagedApp = Join-Path $DataRoot "app"
    if (Test-Path $ManagedApp) { Remove-Item -Recurse -Force $ManagedApp; Write-Ok "Removed $ManagedApp" }
    $ManagedEngines = Join-Path $DataRoot "engines"
    if (Test-Path $ManagedEngines) { Remove-Item -Recurse -Force $ManagedEngines; Write-Ok "Removed managed engines" }
    $ManagedNode = Join-Path $DataRoot "node"
    if (Test-Path $ManagedNode) { Remove-Item -Recurse -Force $ManagedNode; Write-Ok "Removed managed Node.js" }
    $Meta = Join-Path $DataRoot "install_meta.json"
    if (Test-Path $Meta) { Remove-Item -Force $Meta }
    $EngMeta = Join-Path $DataRoot "engine_meta.json"
    if (Test-Path $EngMeta) { Remove-Item -Force $EngMeta }
    if ($Purge -and (Test-Path $DataRoot)) {
        Remove-Item -Recurse -Force $DataRoot
        Write-Ok "Purged all user data in $DataRoot"
    } else {
        Write-Ok "Preserved user data in $DataRoot (pass -Purge to erase)"
    }
    exit 0
}

$TotalSteps = 7
try {

# 1. Detect GPU
$HwSummary = "CPU"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $Gpu = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
    if ($Gpu) { $HwSummary = "NVIDIA CUDA ($($Gpu.Trim()))" }
}
Write-Step "Installing / Updating Splitch on Windows — $HwSummary"

function Assert-Sha256Match([string]$FilePath, [string]$ExpectedHash, [string]$Label) {
    if (-not $ExpectedHash) {
        throw "Missing expected SHA-256 checksum for $Label"
    }
    $Actual = (Get-FileHash -Path $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $Expected = $ExpectedHash.Trim().ToLowerInvariant()
    if ($Actual -ne $Expected) {
        throw "SHA-256 verification failed for $Label (expected $Expected, got $Actual)"
    }
    Write-Ok "SHA-256 verified: $Label"
}

# Verified upstream SHA-256 digests pinned in-script (independent of download origin)
$PinnedUvSha256 = @{
    "0.13.5:uv-x86_64-pc-windows-msvc.zip"  = "a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a"
    "0.13.5:uv-aarch64-pc-windows-msvc.zip" = "09f47edf90726084a70f9426f6e3ca4f46e9bf7cbf23b1716ec64d60b055fae3"
}
$PinnedNodeSha256 = @{
    "22.14.0:node-v22.14.0-win-x64.zip"   = "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217"
    "22.14.0:node-v22.14.0-win-arm64.zip" = "2d71f5f9b2fffa33baa108c07d74b0d24e0c3dd8f441d567772ae0e3dd4b1a22"
}

# 2. Bootstrap pinned uv with SHA-256 verification (no piped remote script)
Show-StepBar 1 $TotalSteps "Checking uv runtime"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Step "Downloading pinned uv v$UvVersion with SHA-256 verification..."
    $OsArch = try { [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() } catch { "X64" }
    $UvArch = if ($OsArch -eq "Arm64") { "aarch64" } elseif ([Environment]::Is64BitOperatingSystem) { "x86_64" } else { "i686" }
    $UvZipName = "uv-$UvArch-pc-windows-msvc.zip"
    $UvBase = "https://github.com/astral-sh/uv/releases/download/$UvVersion"
    $UvPinKey = "${UvVersion}:${UvZipName}"
    $UvExpected = if ($PinnedUvSha256.ContainsKey($UvPinKey)) {
        $PinnedUvSha256[$UvPinKey]
    } else {
        ((Invoke-WebRequest -Uri "$UvBase/$UvZipName.sha256" -UseBasicParsing).Content.Trim() -split "\s+")[0]
    }
    $TmpUvZip = Join-Path $env:TEMP $UvZipName
    $TmpUvDir = Join-Path $env:TEMP "splitch-uv-extract"
    Download-WithProgress -Url "$UvBase/$UvZipName" -OutFile $TmpUvZip -Label $UvZipName
    Assert-Sha256Match -FilePath $TmpUvZip -ExpectedHash $UvExpected -Label $UvZipName
    if (Test-Path $TmpUvDir) { Remove-Item -Recurse -Force $TmpUvDir }
    Expand-Archive -Path $TmpUvZip -DestinationPath $TmpUvDir -Force
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    Get-ChildItem -Path $TmpUvDir -Recurse -Filter "uv*.exe" | ForEach-Object {
        Copy-Item -Force $_.FullName (Join-Path $BinDir $_.Name)
    }
    Remove-Item -Force $TmpUvZip
    Remove-Item -Recurse -Force $TmpUvDir
    $env:PATH = "$BinDir;$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv installation failed or is not on PATH."
}
Write-Ok "uv ready: $(& uv --version)"

# 3. Acquire or Update Source
Show-StepBar 2 $TotalSteps "Preparing Splitch source"
$InstallMode = "managed"
if ($Local) {
    if (-not (Test-Path ".\pyproject.toml")) {
        throw "-Local must be executed from inside a Splitch repository checkout."
    }
    $AppDir = (Get-Location).Path
    $InstallMode = "local"
    Write-Ok "Using local checkout: $AppDir"
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is required. Install Git for Windows (winget install Git.Git) and re-run."
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $AppDir) | Out-Null
    if (Test-Path (Join-Path $AppDir ".git")) {
        Write-Step "Existing installation found — updating ($TargetBranch)..."
        $CurOrigin = (& git -C $AppDir remote get-url origin 2>$null)
        if (-not $CurOrigin) {
            & git -C $AppDir remote add origin $RepoUrl
        } elseif ($CurOrigin -in @("https://github.com/splitch-ai/splitch.git", "https://github.com/splitch/splitch.git") -or $Repo) {
            & git -C $AppDir remote set-url origin $RepoUrl
        }
        $OldSha = (& git -C $AppDir rev-parse --short HEAD)
        & git -C $AppDir fetch --quiet origin $TargetBranch
        & git -C $AppDir pull --ff-only origin $TargetBranch
        $NewSha = (& git -C $AppDir rev-parse --short HEAD)
        if ($OldSha -eq $NewSha) {
            Write-Ok "Already up to date at $TargetBranch@$NewSha"
        } else {
            Write-Ok "Updated source: $OldSha -> $NewSha"
        }
    } else {
        Write-Step "Cloning Splitch ($TargetBranch) into $AppDir..."
        & git clone --depth 1 --branch $TargetBranch $RepoUrl $AppDir
        Write-Ok "Cloned into $AppDir"
    }
}

# 4. Sync Python Environment via uv
Show-StepBar 3 $TotalSteps "Syncing Python environment"
Push-Location $AppDir
try {
    if (-not $AgentOnly -and -not $NoDesktop) {
        & uv sync --python $Python --no-dev --extra desktop
    } else {
        & uv sync --python $Python --no-dev
    }
} finally {
    Pop-Location
}
Write-Ok "Python backend dependencies ready"

# 5. Ensure Node.js 20+ & Build Dashboard
Show-StepBar 4 $TotalSteps "Dashboard build"
function Ensure-Node {
    $NeedNode = $true
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $Major = [int]((& node -p "process.versions.node.split('.')[0]") 2>$null)
        if ($Major -ge 20) {
            Write-Ok "Node.js ready: $(& node --version)"
            $NeedNode = $false
        }
    }
    if (-not $NeedNode) { return }

    $ManagedNode = Join-Path $DataRoot "node"
    $ManagedExe = Join-Path $ManagedNode "node.exe"
    if (-not (Test-Path $ManagedExe)) {
        $OsArch = try { [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() } catch { "X64" }
        $Arch = if ($OsArch -eq "Arm64") { "arm64" } elseif ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
        $ZipName = "node-v$NodeVersion-win-$Arch"
        $ZipFile = "$ZipName.zip"
        $Url = "https://nodejs.org/dist/v$NodeVersion/$ZipFile"
        $NodePinKey = "${NodeVersion}:${ZipFile}"
        $ExpectedSha = if ($PinnedNodeSha256.ContainsKey($NodePinKey)) {
            $PinnedNodeSha256[$NodePinKey]
        } else {
            $ShaText = (Invoke-WebRequest -Uri "https://nodejs.org/dist/v$NodeVersion/SHASUMS256.txt" -UseBasicParsing).Content
            $ShaLine = ($ShaText -split "`n" | Where-Object { $_ -match "\s+$([regex]::Escape($ZipFile))\s*$" } | Select-Object -First 1)
            if ($ShaLine) { ($ShaLine.Trim() -split "\s+")[0] } else { "" }
        }
        $TmpZip = Join-Path $env:TEMP $ZipFile
        $TmpExtract = Join-Path $env:TEMP "$ZipName-extract"
        Download-WithProgress -Url $Url -OutFile $TmpZip -Label $ZipFile
        Assert-Sha256Match -FilePath $TmpZip -ExpectedHash $ExpectedSha -Label $ZipFile
        if (Test-Path $TmpExtract) { Remove-Item -Recurse -Force $TmpExtract }
        Expand-Archive -Path $TmpZip -DestinationPath $TmpExtract -Force
        if (Test-Path $ManagedNode) { Remove-Item -Recurse -Force $ManagedNode }
        Move-Item -Path (Join-Path $TmpExtract $ZipName) -Destination $ManagedNode
        Remove-Item -Force $TmpZip
        Remove-Item -Recurse -Force $TmpExtract
    }
    $env:PATH = "$ManagedNode;$env:PATH"
    Write-Ok "Using managed Node.js: $(& node --version)"
}

$DashDir = Join-Path $AppDir "dashboard"
if ($AgentOnly) {
    Write-Ok "Agent-only mode: skipped Node.js & dashboard UI build"
} elseif ($NoUiBuild) {
    Write-Ok "Skipped dashboard UI build (-NoUiBuild)"
} elseif (Test-Path (Join-Path $DashDir "package.json")) {
    Ensure-Node
    Write-Step "Building Splitch dashboard UI..."
    Push-Location $DashDir
    try {
        if (Test-Path "package-lock.json") {
            & npm ci --no-audit --no-fund
        } else {
            & npm install --no-audit --no-fund
        }
        & npm run build
    } finally {
        Pop-Location
    }
    Write-Ok "Dashboard UI built cleanly"
}

# 6. Install Global `splitch.cmd` Shim & Update User PATH
Show-StepBar 5 $TotalSteps "Installing CLI shim"
New-Item -ItemType Directory -Force -Path $BinDir, $DataRoot | Out-Null
$VenvExe = Join-Path $AppDir ".venv\Scripts\splitch.exe"
$ShimPath = Join-Path $BinDir "splitch.cmd"
"@echo off`r`n`"$VenvExe`" %*`r`n" | Set-Content -Path $ShimPath -Encoding ASCII
Write-Ok "Installed CLI shim: $ShimPath"

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
if (-not ($UserPath.Split(";") -contains $BinDir)) {
    $NewPath = if ($UserPath) { "$BinDir;$UserPath" } else { $BinDir }
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    $env:PATH = "$BinDir;$env:PATH"
    Write-Ok "Added $BinDir to User PATH"
}

$MetaPath = Join-Path $DataRoot "install_meta.json"
$NowTs = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$Meta = @{
    install_dir  = $AppDir
    bin_path     = $ShimPath
    mode         = $InstallMode
    repo_url     = $RepoUrl
    branch       = $TargetBranch
    python       = $Python
    agent_only   = [bool]$AgentOnly
    installed_at = $NowTs
    updated_at   = $NowTs
}
$Meta | ConvertTo-Json -Depth 4 | Set-Content -Path $MetaPath -Encoding UTF8

# 7. Install / Update Inference Engine Backend
Show-StepBar 6 $TotalSteps "Inference & media engines"
$EngMetaPath = Join-Path $DataRoot "engine_meta.json"
$env:SPLITCH_DATA_ROOT = $DataRoot
if ($TargetEngine -eq "none") {
    Write-Ok "Skipped inference engine install (-Engine none)"
} elseif ($TargetEngine -in @("llama.cpp", "ollama")) {
    Write-Step "Installing / updating inference engine ($TargetEngine, backend=$TargetBackend)..."
    & $VenvExe engine install $TargetEngine --backend $TargetBackend --bin-dir $BinDir
} else {
    if (Test-Path $EngMetaPath) {
        Write-Step "Checking managed inference engine for updates..."
        & $VenvExe engine update --backend $TargetBackend --bin-dir $BinDir
    } elseif (-not (Get-Command llama-server -ErrorAction SilentlyContinue) -and -not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Step "No local inference engine found — installing managed llama.cpp (backend=$TargetBackend)..."
        & $VenvExe engine install llama.cpp --backend $TargetBackend --bin-dir $BinDir
    } else {
        Write-Ok "Existing local inference engine detected (pass -Engine llama.cpp to install Splitch-managed llama.cpp)"
    }
}

$SdMetaPath = Join-Path $DataRoot "sd_engine_meta.json"
if ($WithMedia -or (Test-Path $SdMetaPath)) {
    Write-Step "Installing / updating managed media engine (sd.cpp, backend=$TargetBackend)..."
    & $VenvExe engine install sd.cpp --backend $TargetBackend --bin-dir $BinDir
}

Show-StepBar 7 $TotalSteps "Complete"
if (-not $AgentOnly -and -not $NoDesktop) {
    try {
        & $VenvExe desktop install | Out-Null
        Write-Ok "Installed Start Menu & Desktop shortcuts"
    } catch {
        Write-Warn "Skipped desktop shortcut install (run 'splitch desktop install' later)"
    }
}
if ($Join) {
    Write-Step "Registering and starting splitch-agent -> $Join..."
    $AgentArgs = @("agent", "--join", $Join)
    if ($JoinToken) { $AgentArgs += @("--join-token", $JoinToken) }
    if ($NodeId) { $AgentArgs += @("--id", $NodeId) }
    if ($NodeName) { $AgentArgs += @("--name", $NodeName) }
    if ($Role) { $AgentArgs += @("--role", $Role) }
    if ($YieldOnGaming) { $AgentArgs += "--yield-on-gaming" }
    if ($YieldGpus) { $AgentArgs += @("--yield-gpus", $YieldGpus) }
    if ($EnableTools) { $AgentArgs += "--enable-tools" }
    & $VenvExe @($AgentArgs + "--join-only")
    Start-Process -FilePath $VenvExe -ArgumentList ($AgentArgs | Where-Object { $_ -ne "--join-token" -and $_ -ne $JoinToken -and $_ -ne "--join" -and $_ -ne $Join }) -WindowStyle Hidden
    Write-Ok "Registered node and launched splitch-agent in background"
}
Write-Host ""
Write-Step "Splitch is ready!"
if ($AgentOnly) {
    Write-Host "  Next step: splitch agent --yield-on-gaming --join http://<ROUTER_IP>:8080"
} else {
    Write-Host "  Next steps:"
    Write-Host "    splitch doctor --check-updates   # verify GPU, engines, and upstream updates"
    Write-Host "    splitch up                       # start local agent + router + UI on http://localhost:8080"
    Write-Host "    splitch update --with-engine     # update Splitch + inference engine anytime"
}
} finally {
    Set-OscProgress 0 0
    Write-Progress -Activity "Splitch Installer" -Completed
}
