param(
    [string]$Server = "https://162-35-190-30.sslip.io/avc",
    [string]$EnrollmentToken = "",
    [string]$Label = $env:COMPUTERNAME,
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$AgentVersion = "0.3.0"
$Root = Join-Path $env:ProgramData "AImanVisualCopilot"
$ConfigPath = Join-Path $Root "config.json"
$LogPath = Join-Path $Root "observer.log"
$InstalledScript = Join-Path $Root "observer.ps1"
$TaskName = "AImanVisualCopilot Observer"

function Write-Log([string]$Message) {
    try {
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        $line = "$(Get-Date -Format o) $Message"
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
        $lines = @(Get-Content $LogPath -ErrorAction SilentlyContinue)
        if ($lines.Count -gt 1500) { $lines[-800..-1] | Set-Content $LogPath -Encoding UTF8 }
    } catch {}
}

if ($Uninstall) {
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
    try { Remove-Item -Recurse -Force $Root -ErrorAction SilentlyContinue } catch {}
    Write-Output "AImanVisualCopilot observer removed."
    exit 0
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null

if ($Install) {
    Copy-Item -Force $MyInvocation.MyCommand.Path $InstalledScript
}

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

if (-not ("AVC.Native" -as [type])) {
Add-Type @"
using System;
using System.Runtime.InteropServices;
namespace AVC {
  public static class Native {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT lpPoint);
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("kernel32.dll")] public static extern bool FreeConsole();
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  }
}
"@
}

# Detach the long-running observer from any console host. This keeps AVC alive even if
# the installer/Terminal window is closed and prevents a persistent CMD/PowerShell box.
if (-not $Install) {
    try {
        if ([AVC.Native]::GetConsoleWindow() -ne [IntPtr]::Zero) { [void][AVC.Native]::FreeConsole() }
    } catch {}
}

function Invoke-JsonPost([string]$Url, [hashtable]$Body, [string]$Token = "") {
    $headers = @{}
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }
    $json = $Body | ConvertTo-Json -Depth 20 -Compress
    # Windows PowerShell 5.1 may send a .NET string using a legacy code page even
    # when Content-Type is application/json. AVC frames frequently contain Chinese
    # UI text, so always send explicit UTF-8 bytes.
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    try {
        return Invoke-RestMethod -Uri $Url -Method Post -ContentType "application/json; charset=utf-8" -Headers $headers -Body $bytes -TimeoutSec 15
    } catch {
        $detail = ""
        try {
            if ($_.Exception.Response) {
                $stream = $_.Exception.Response.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader($stream)
                    $detail = $reader.ReadToEnd()
                    $reader.Dispose()
                }
            }
        } catch {}
        if ($detail) { throw ("HTTP POST failed: " + $_.Exception.Message + " body=" + $detail) }
        throw
    }
}

function Get-ControlState([string]$Url, [string]$Token) {
    $headers = @{ "Authorization" = "Bearer $Token" }
    return Invoke-RestMethod -Uri $Url -Method Get -Headers $headers -TimeoutSec 10
}

function Get-DeviceId {
    try {
        $guid = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid -ErrorAction Stop).MachineGuid
        if ($guid) { return "windows-$guid" }
    } catch {}
    return "windows-$env:COMPUTERNAME-$env:USERNAME"
}

function Load-Config {
    if (Test-Path $ConfigPath) {
        try { return Get-Content $ConfigPath -Raw | ConvertFrom-Json } catch {}
    }
    return $null
}

function Save-Config($Config) {
    $Config | ConvertTo-Json -Depth 10 | Set-Content $ConfigPath -Encoding UTF8
    try {
        $acl = Get-Acl $ConfigPath
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($env:USERNAME,"FullControl","Allow")
        $acl.SetAccessRule($rule)
        Set-Acl $ConfigPath $acl
    } catch {}
}

function Register-Source {
    if (-not $EnrollmentToken) { throw "EnrollmentToken is required for first registration." }
    $body = @{
        enrollment_token = $EnrollmentToken
        label = $Label
        device_id = Get-DeviceId
        hostname = $env:COMPUTERNAME
        platform = "windows"
        agent_version = $AgentVersion
        metadata = @{
            username = $env:USERNAME
            os = [Environment]::OSVersion.VersionString
            observer = "uia+screenshot"
        }
    }
    $r = Invoke-JsonPost "$Server/api/v1/register" $body
    if (-not $r.ok) { throw "registration failed" }
    $cfg = [pscustomobject]@{
        server = $Server
        source_id = $r.source_id
        source_token = $r.source_token
        label = $Label
        registered_at = (Get-Date).ToString("o")
    }
    Save-Config $cfg
    return $cfg
}

function Get-ForegroundInfo {
    $h = [AVC.Native]::GetForegroundWindow()
    if ($h -eq [IntPtr]::Zero) { return $null }
    [uint32]$processId = 0
    [void][AVC.Native]::GetWindowThreadProcessId($h, [ref]$processId)
    $p = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    $rect = New-Object AVC.Native+RECT
    [void][AVC.Native]::GetWindowRect($h, [ref]$rect)
    return [pscustomobject]@{
        Handle = $h
        Pid = $processId
        App = $p.ProcessName
        Title = $p.MainWindowTitle
        Left = $rect.Left
        Top = $rect.Top
        Width = [Math]::Max(1, $rect.Right - $rect.Left)
        Height = [Math]::Max(1, $rect.Bottom - $rect.Top)
    }
}

function Get-CursorInfo {
    try {
        $pt = New-Object AVC.Native+POINT
        if ([AVC.Native]::GetCursorPos([ref]$pt)) { return @{ x=[int]$pt.X; y=[int]$pt.Y } }
    } catch {}
    return @{}
}

function Get-InputEvents([hashtable]$State) {
    # Read-only polling. The observer records edge transitions and never injects input.
    $items = New-Object System.Collections.Generic.List[object]
    $cursor = Get-CursorInfo
    $keys = @{
        1=@("click","left"); 2=@("click","right"); 4=@("click","middle")
        8=@("key","Backspace"); 9=@("key","Tab"); 13=@("key","Enter"); 16=@("key","Shift")
        17=@("key","Control"); 18=@("key","Alt"); 27=@("key","Escape"); 32=@("key","Space")
        33=@("key","PageUp"); 34=@("key","PageDown"); 35=@("key","End"); 36=@("key","Home")
        37=@("key","ArrowLeft"); 38=@("key","ArrowUp"); 39=@("key","ArrowRight"); 40=@("key","ArrowDown")
        46=@("key","Delete")
    }
    foreach ($vk in 1..90) {
        if (-not $keys.ContainsKey($vk) -and -not (($vk -ge 48 -and $vk -le 57) -or ($vk -ge 65 -and $vk -le 90))) { continue }
        $raw = [int][AVC.Native]::GetAsyncKeyState($vk)
        $down = ($raw -band 0x8000) -ne 0
        $pressed = ($raw -band 0x0001) -ne 0
        $wasDown = [bool]$State[$vk]
        if (($pressed -or ($down -and -not $wasDown))) {
            if ($keys.ContainsKey($vk)) { $kind=[string]$keys[$vk][0]; $name=[string]$keys[$vk][1] }
            else { $kind="key"; $name="character" }
            $items.Add(@{
                type=("input_" + $kind); kind=$kind; key=$(if ($kind -eq "key") { $name } else { "" })
                button=$(if ($kind -eq "click") { $name } else { "" })
                x=$(if ($cursor.ContainsKey("x")) { $cursor.x } else { $null })
                y=$(if ($cursor.ContainsKey("y")) { $cursor.y } else { $null })
                timestamp_unix=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            })
        }
        $State[$vk] = $down
    }
    return @($items)
}

function Get-ValuePatternText($Element) {
    try {
        $pattern = $null
        if ($Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
            return $pattern.Current.Value
        }
    } catch {}
    return ""
}

function Get-UiaSnapshot([IntPtr]$Handle, [int]$MaxElements = 220, [int]$MaxDepth = 5, [int]$BudgetMs = 900) {
    # Avoid AutomationElement.FindAll(TreeScope.Subtree): Chromium and some complex
    # apps can make that call block for a long time. Walk a bounded ControlView tree
    # instead so the observer remains responsive and heartbeats keep flowing.
    $items = New-Object System.Collections.Generic.List[object]
    $texts = New-Object System.Collections.Generic.List[string]
    $url = ""
    $activeTab = ""
    $focus = @{}
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
        if (-not $root) { return @{ elements=@(); text=""; url=""; active_tab=""; focus=@{}; truncated=$false } }
        try {
            $fe = [System.Windows.Automation.AutomationElement]::FocusedElement
            if ($fe) {
                $fc = $fe.Current
                $ft = [string]$fc.ControlType.ProgrammaticName
                if ($ft.StartsWith("ControlType.")) { $ft = $ft.Substring(12) }
                $focus = @{ role=$ft; name=[string]$fc.Name; automation_id=[string]$fc.AutomationId }
            }
        } catch {}
        $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $queue = New-Object System.Collections.Queue
        $queue.Enqueue([pscustomobject]@{ e=$root; depth=0 })
        $seenText = @{}
        $visited = 0
        $truncated = $false
        while ($queue.Count -gt 0) {
            if ($visited -ge $MaxElements -or $sw.ElapsedMilliseconds -ge $BudgetMs) { $truncated = $true; break }
            $node = $queue.Dequeue()
            $e = $node.e
            $depth = [int]$node.depth
            $visited++
            try {
                $c = $e.Current
                if (-not $c.IsOffscreen) {
                    $name = [string]$c.Name
                    $aid = [string]$c.AutomationId
                    $type = [string]$c.ControlType.ProgrammaticName
                    if ($type.StartsWith("ControlType.")) { $type = $type.Substring(12) }
                    $r = $c.BoundingRectangle
                    if ($name -and -not $seenText.ContainsKey($name)) {
                        $seenText[$name] = $true
                        if ($texts.Count -lt 180) { $texts.Add($name) }
                    }
                    $value = ""
                    if ($type -eq "Edit") {
                        $value = Get-ValuePatternText $e
                        $n = $name.ToLowerInvariant()
                        if ($value -match '^(https?|file|edge|chrome|about):' -and ($n -match 'address|search|地址|网址|搜尋|搜索|omnibox')) { $url = $value }
                    }
                    if ($type -eq "TabItem" -and $name) {
                        try {
                            $sel = $null
                            if ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$sel) -and $sel.Current.IsSelected) {
                                $activeTab = $name
                            }
                        } catch {}
                    }
                    if ($items.Count -lt 180 -and ($name -or $aid -or $type -in @("Button","Edit","TabItem","MenuItem","Hyperlink","Text","ListItem"))) {
                        $items.Add([pscustomobject]@{
                            role = $type
                            name = $name
                            automation_id = $aid
                            value = if ($value.Length -le 300) { $value } else { $value.Substring(0,300) }
                            x = [int]$r.X; y = [int]$r.Y; width = [int]$r.Width; height = [int]$r.Height
                            enabled = [bool]$c.IsEnabled
                        })
                    }
                }
            } catch {}
            if ($depth -lt $MaxDepth -and $sw.ElapsedMilliseconds -lt $BudgetMs) {
                try {
                    $child = $walker.GetFirstChild($e)
                    $siblings = 0
                    while ($child -and $siblings -lt 80 -and $queue.Count -lt ($MaxElements * 2)) {
                        $queue.Enqueue([pscustomobject]@{ e=$child; depth=$depth+1 })
                        $siblings++
                        $child = $walker.GetNextSibling($child)
                    }
                } catch {}
            }
        }
    } catch {
        Write-Log "UIA snapshot error: $($_.Exception.Message)"
    } finally {
        $sw.Stop()
    }
    $joined = ($texts -join "`n")
    if ($joined.Length -gt 24000) { $joined = $joined.Substring(0,24000) }
    return @{ elements=$items; text=$joined; url=$url; active_tab=$activeTab; focus=$focus; truncated=$truncated; elapsed_ms=$sw.ElapsedMilliseconds }
}

# Load WinForms only after defining functions; SystemInformation helps clip multi-monitor bounds.
Add-Type -AssemblyName System.Windows.Forms

function Get-WindowJpeg($Info, [int]$MaxWidth = 1280, [long]$Quality = 48) {
    try {
        $v = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $left = [Math]::Max($Info.Left, $v.Left)
        $top = [Math]::Max($Info.Top, $v.Top)
        $right = [Math]::Min($Info.Left + $Info.Width, $v.Right)
        $bottom = [Math]::Min($Info.Top + $Info.Height, $v.Bottom)
        $w = [Math]::Max(1, $right - $left)
        $h = [Math]::Max(1, $bottom - $top)
        if ($w -lt 40 -or $h -lt 40) { return $null }
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($left, $top, 0, 0, (New-Object System.Drawing.Size($w,$h)))
        $g.Dispose()
        $outBmp = $bmp
        $outW = $w; $outH = $h
        if ($w -gt $MaxWidth) {
            $outW = $MaxWidth
            $outH = [int][Math]::Round($h * ($MaxWidth / [double]$w))
            $scaled = New-Object System.Drawing.Bitmap($outW, $outH)
            $sg = [System.Drawing.Graphics]::FromImage($scaled)
            $sg.DrawImage($bmp, 0, 0, $outW, $outH)
            $sg.Dispose(); $bmp.Dispose(); $outBmp = $scaled
        }
        $dHash = ""
        try {
            $hb = New-Object System.Drawing.Bitmap(9,8)
            $hg = [System.Drawing.Graphics]::FromImage($hb)
            $hg.DrawImage($outBmp, 0, 0, 9, 8)
            $hg.Dispose()
            $bits = New-Object System.Text.StringBuilder
            for ($yy=0; $yy -lt 8; $yy++) {
                for ($xx=0; $xx -lt 8; $xx++) {
                    $ca = $hb.GetPixel($xx,$yy); $cb = $hb.GetPixel($xx+1,$yy)
                    $ga = 299*[int]$ca.R + 587*[int]$ca.G + 114*[int]$ca.B
                    $gb = 299*[int]$cb.R + 587*[int]$cb.G + 114*[int]$cb.B
                    [void]$bits.Append($(if ($ga -gt $gb) { "1" } else { "0" }))
                }
            }
            $parts = New-Object System.Collections.Generic.List[string]
            $bin = $bits.ToString()
            for ($i=0; $i -lt 64; $i+=4) { $parts.Add(([Convert]::ToInt32($bin.Substring($i,4),2)).ToString("x")) }
            $dHash = $parts -join ""
            $hb.Dispose()
        } catch {}
        $ms = New-Object System.IO.MemoryStream
        $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq "image/jpeg" } | Select-Object -First 1
        $enc = New-Object System.Drawing.Imaging.EncoderParameters(1)
        $enc.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, $Quality)
        $outBmp.Save($ms, $codec, $enc)
        $bytes = $ms.ToArray()
        $ms.Dispose(); $outBmp.Dispose()
        $sha = [System.BitConverter]::ToString(([System.Security.Cryptography.SHA256]::Create()).ComputeHash($bytes)).Replace("-","").ToLowerInvariant()
        return [pscustomobject]@{ Bytes=$bytes; Base64=[Convert]::ToBase64String($bytes); Sha=$sha; DHash=$dHash; Width=$outW; Height=$outH }
    } catch {
        Write-Log "screenshot error: $($_.Exception.Message)"
        return $null
    }
}

function Get-HexHamming([string]$A, [string]$B) {
    if (-not $A -or -not $B -or $A.Length -ne $B.Length) { return 64 }
    $counts = @(0,1,1,2,1,2,2,3,1,2,2,3,2,3,3,4)
    $d = 0
    for ($i=0; $i -lt $A.Length; $i++) {
        try {
            $x = [Convert]::ToInt32($A[$i].ToString(),16) -bxor [Convert]::ToInt32($B[$i].ToString(),16)
            $d += $counts[$x]
        } catch { return 64 }
    }
    return $d
}

function Get-Sha256String([string]$s) {
    $b = [Text.Encoding]::UTF8.GetBytes($s)
    return [BitConverter]::ToString(([Security.Cryptography.SHA256]::Create()).ComputeHash($b)).Replace("-","").ToLowerInvariant()
}

$config = Load-Config
if (-not $config) { $config = Register-Source }
$Server = $config.server.TrimEnd('/')
$SourceToken = [string]$config.source_token

if ($Install) {
    $LauncherPath = Join-Path $Root "observer-launcher.vbs"

    # Stop the previous scheduled instance and terminate only stale AVC observer processes.
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like "*$InstalledScript*" } |
            ForEach-Object { try { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null } catch {} }
    } catch {}

    Copy-Item -Force $MyInvocation.MyCommand.Path $InstalledScript

    # WScript has no console window. It launches the observer hidden and waits for it, so
    # Task Scheduler still supervises the actual long-running observer process.
    $escapedScript = $InstalledScript.Replace('"','""')
    $vbs = @"
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$escapedScript"""
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
"@
    Set-Content -Path $LauncherPath -Value $vbs -Encoding ASCII

    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //NoLogo `"$LauncherPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    try { $settings.Hidden = $true } catch {}
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Read-only Windows observer for AImanVisualCopilot" -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Milliseconds 900
    $state = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
    Write-Output "AImanVisualCopilot observer $AgentVersion installed. Scheduled task state: $state"
    exit 0
}

Write-Log "observer starting source=$($config.source_id) server=$Server"
$seq = 0
$lastSemantic = ""
$lastShot = ""
$lastUpload = [DateTime]::MinValue
$lastUia = [DateTime]::MinValue
$uiaCache = @{ elements=@(); text=""; url=""; active_tab=""; focus=@{} }
$previous = @{ app=""; title=""; url=""; activeTab=""; textHash=""; focusKey=""; visualHash="" }
$lastHeartbeat = Get-Date
$lastControlPoll = [DateTime]::MinValue
$monitoringEnabled = $true
$lastLoggedMonitoring = $null
$inputState = @{}

while ($true) {
    $controlNow = Get-Date
    if (($controlNow - $lastControlPoll).TotalSeconds -ge 3) {
        try {
            $control = Get-ControlState "$Server/api/v1/control" $SourceToken
            $monitoringEnabled = [bool]$control.monitoring_enabled
            $lastControlPoll = $controlNow
            if ($null -eq $lastLoggedMonitoring -or $lastLoggedMonitoring -ne $monitoringEnabled) {
                Write-Log ("monitoring state=" + $(if ($monitoringEnabled) { "on" } else { "off" }))
                $lastLoggedMonitoring = $monitoringEnabled
            }
        } catch {
            Write-Log "control poll error: $($_.Exception.Message)"
            $lastControlPoll = $controlNow
        }
    }
    if (-not $monitoringEnabled) {
        Start-Sleep -Milliseconds 600
        continue
    }
    try {
        $info = Get-ForegroundInfo
        if (-not $info) { Start-Sleep -Milliseconds 700; continue }
        $now = Get-Date
        $needUia = (($now - $lastUia).TotalMilliseconds -ge 1800) -or ($info.App -ne $previous.app) -or ($info.Title -ne $previous.title)
        if ($needUia) {
            $isBrowser = $info.App -in @("chrome","msedge","firefox","brave","opera")
            if ($isBrowser) { $uiaCache = Get-UiaSnapshot $info.Handle 140 4 700 }
            else { $uiaCache = Get-UiaSnapshot $info.Handle 220 5 900 }
            $lastUia = $now
        }
        $url = [string]$uiaCache.url
        $text = [string]$uiaCache.text
        $activeTab = [string]$uiaCache.active_tab
        $focus = if ($uiaCache.focus) { $uiaCache.focus } else { @{} }
        $focusKey = "$($focus.role)|$($focus.name)|$($focus.automation_id)"
        $cursor = Get-CursorInfo
        $inputEvents = @(Get-InputEvents $inputState)
        $surface = if ($url -or $info.App -in @("chrome","msedge","firefox","brave","opera")) { "browser" } else { "desktop" }
        $textHash = Get-Sha256String $text
        $semantic = Get-Sha256String "$($info.App)`n$($info.Title)`n$url`n$activeTab`n$textHash"
        $shot = Get-WindowJpeg $info
        $visualDistance = if ($shot -and $previous.visualHash) { Get-HexHamming $previous.visualHash $shot.DHash } else { 64 }
        $significantVisual = $shot -and $previous.visualHash -and ($visualDistance -ge 10)
        $shotChanged = $shot -and ((-not $previous.visualHash) -or ($visualDistance -ge 3))
        $semanticChanged = $semantic -ne $lastSemantic
        $elapsed = ($now - $lastUpload).TotalSeconds
        $shouldUpload = ($inputEvents.Count -gt 0) -or $semanticChanged -or $significantVisual -or ($shotChanged -and $elapsed -ge 1.5) -or ($elapsed -ge 5.0)
        if ($shouldUpload) {
            $changes = New-Object System.Collections.Generic.List[object]
            if ($previous.app -and $previous.app -ne $info.App) { $changes.Add(@{ type="app_changed"; from=$previous.app; to=$info.App }) }
            if ($previous.title -and $previous.title -ne $info.Title) { $changes.Add(@{ type="window_changed"; from=$previous.title; to=$info.Title }) }
            if ($previous.url -ne $url -and ($previous.url -or $url)) { $changes.Add(@{ type="url_changed"; from=$previous.url; to=$url }) }
            if ($previous.activeTab -ne $activeTab -and ($previous.activeTab -or $activeTab)) { $changes.Add(@{ type="active_tab_changed"; from=$previous.activeTab; to=$activeTab }) }
            if ($previous.focusKey -and $previous.focusKey -ne $focusKey) { $changes.Add(@{ type="focus_changed"; from=$previous.focusKey; to=$focusKey }) }
            if ($significantVisual) { $changes.Add(@{ type="significant_visual_change"; distance=$visualDistance }) }
            if ($previous.textHash -and $previous.textHash -ne $textHash) { $changes.Add(@{ type="visible_text_changed" }) }
            $seq++
            $body = @{
                seq = $seq
                timestamp_unix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
                agent_version = $AgentVersion
                app = $info.App
                window_title = $info.Title
                url = $url
                active_tab = $activeTab
                focus = $focus
                cursor = $cursor
                visual_hash = if ($shot) { $shot.DHash } else { "" }
                surface = $surface
                summary = "$($info.App) — $($info.Title)"
                visible_text = $text
                uia = @{ elements = $uiaCache.elements }
                dom = @{}
                events = $inputEvents
                changes = $changes
                metadata = @{ capture="active_window"; observer="uia+screenshot"; pid=$info.Pid; visual_hamming_from_previous=$visualDistance }
            }
            if ($shot) {
                $body.screenshot_base64 = $shot.Base64
                $body.screenshot_mime = "image/jpeg"
                $body.width = $shot.Width
                $body.height = $shot.Height
            }
            $r = Invoke-JsonPost "$Server/api/v1/frame" $body $SourceToken
            if ($r.ok) {
                $lastSemantic = $semantic
                if ($shot) { $lastShot = $shot.Sha }
                $lastUpload = $now
                $previous = @{ app=$info.App; title=$info.Title; url=$url; activeTab=$activeTab; textHash=$textHash; focusKey=$focusKey; visualHash=$(if ($shot) { $shot.DHash } else { $previous.visualHash }) }
            }
        }
    } catch {
        Write-Log "loop error: $($_.Exception.Message)"
    }
    # Heartbeat is deliberately outside the frame-upload try block. A malformed or
    # rejected frame must never make a healthy observer appear offline.
    $hbNow = Get-Date
    if (($hbNow - $lastHeartbeat).TotalSeconds -ge 8) {
        try {
            [void](Invoke-JsonPost "$Server/api/v1/heartbeat" @{} $SourceToken)
            $lastHeartbeat = $hbNow
        } catch {
            Write-Log "heartbeat error: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Milliseconds 600
}
