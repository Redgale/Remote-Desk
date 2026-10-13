# PowerShell variable names are case-insensitive, so $Conf and $conf are the SAME variable.
# This finds such accidental clashes, which neither the parser nor PSScriptAnalyzer reports.
#   pwsh -File tests/check_ps1.ps1 setup.ps1
$t = $null; $e = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$t, [ref]$e)
$names = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true) |
  ForEach-Object { $_.VariablePath.UserPath })
$byLower = @{}
foreach ($n in $names) {
  $k = $n.ToLowerInvariant()
  if (-not $byLower.ContainsKey($k)) { $byLower[$k] = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal) }
  [void]$byLower[$k].Add($n)
}
$clash = $byLower.Values | Where-Object { $_.Count -gt 1 } | ForEach-Object { ($_ | Sort-Object) -join " vs " }
if ($clash) { $clash | ForEach-Object { "CASE CLASH: $_" } } else { "no case clashes among $($byLower.Count) variable names" }
if ($clash) { exit 1 }
