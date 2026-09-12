param([Parameter(Mandatory=$true)][string]$Path)
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($Path,[ref]$tokens,[ref]$errors) | Out-Null
if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_.Message }
    exit 1
}
Write-Output "PowerShell syntax OK; tokens=$($tokens.Count)"
