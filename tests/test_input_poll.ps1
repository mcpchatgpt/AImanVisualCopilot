$ErrorActionPreference = 'Stop'
Add-Type @"
using System.Runtime.InteropServices;
public static class AVCInputTest {
  [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT lpPoint);
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
}
"@
function Get-CursorInfo {
    $pt = New-Object AVCInputTest+POINT
    if ([AVCInputTest]::GetCursorPos([ref]$pt)) { return @{ x=[int]$pt.X; y=[int]$pt.Y } }
    return @{}
}
function Test-InputEvents([hashtable]$State) {
    $items = New-Object System.Collections.Generic.List[object]
    $cursor = Get-CursorInfo
    foreach ($vk in 1..90) {
        $raw = [int][AVCInputTest]::GetAsyncKeyState([int]$vk)
        $down = ($raw -band 0x8000) -ne 0
        $pressed = ($raw -band 0x0001) -ne 0
        $wasDown = [bool]$State[[int]$vk]
        if ($pressed -or ($down -and -not $wasDown)) {
            $items.Add([pscustomobject]@{ kind='edge'; x=$cursor['x']; y=$cursor['y'] })
        }
        $State[[int]$vk] = $down
    }
    return $items.ToArray()
}
$state = @{}
$result = @(Test-InputEvents $state)
Write-Output "Input poll OK; events=$($result.Count); states=$($state.Count)"
