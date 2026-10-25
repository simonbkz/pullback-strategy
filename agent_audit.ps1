# Daily agent audit: deterministic dossier -> headless Claude Code judgment -> report.
# Exit code: 0 OK, 1 WARN, 2 ALERT, 3 runner failure. Scheduled as fx-edge-agent-audit.
$ErrorActionPreference = "Continue"
$root = "C:\Users\Administrator\fx-edge"; Set-Location $root
$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
$dir = Join-Path $root "reports\agent_audit"; New-Item -ItemType Directory -Force -Path (Join-Path $dir "scratch") | Out-Null
$dossier = Join-Path $dir "$stamp.dossier.txt"; $report = Join-Path $dir "$stamp.md"; $hist = Join-Path $dir "history.log"

# 1. dossier (read-only python)
& "$root\.venv\Scripts\python.exe" "$root\scripts\audit_checks.py" 2>&1 | Where-Object { "$_" -notmatch "DeprecationWarning|utcnow\(\)" } | Out-File -FilePath $dossier -Encoding utf8
if (-not (Test-Path $dossier) -or (Get-Item $dossier).Length -lt 500) { "$stamp RUNNER_FAIL dossier empty" | Add-Content $hist; exit 3 }

# 2. newest Claude Code binary shipped with the VS Code extension
$ext = Get-ChildItem "$env:USERPROFILE\.vscode\extensions" -Directory -Filter "anthropic.claude-code-*" |
    Sort-Object { [version]($_.Name -replace '^anthropic\.claude-code-','' -replace '-win32-x64$','') } -Descending | Select-Object -First 1
$claude = Join-Path $ext.FullName "resources\native-binary\claude.exe"
if (-not (Test-Path $claude)) { "$stamp RUNNER_FAIL no claude binary" | Add-Content $hist; exit 3 }

# 3. judgment
$system = Get-Content (Join-Path $root "agent_audit\AUDIT_PROMPT.md") -Raw
$user = "Run today's audit. Dossier: $dossier . Write nothing except your final answer; the runner saves it to $report ."
$sw = [Diagnostics.Stopwatch]::StartNew()
$out = & $claude -p $user --append-system-prompt $system --output-format text --max-turns 40 `
    --allowedTools "Read,Grep,Glob,PowerShell" 2>&1 | Where-Object { "$_" -notmatch "permissions.allow entries|hasTrustDialogAccepted|Run Claude Code interactively" }
$text = ($out | ForEach-Object { "$_" }) -join "`n"
$text | Out-File -FilePath $report -Encoding utf8
$status = if ($text -match "(?m)^\s*\**STATUS:?\**\s*\**(OK|WARN|ALERT)") { $Matches[1] }
          elseif ($text -match "STATUS[:\s*]*\b(OK|WARN|ALERT)\b") { $Matches[1] }
          elseif ($text.Length -gt 400) { "WARN" }   # report produced but no status line: needs a human look, not a runner failure
          else { "UNKNOWN" }
$summary = (($text -split "`n") | Where-Object { $_ -and $_ -notmatch "^STATUS:" } | Select-Object -First 1)
"$stamp $status $([int]$sw.Elapsed.TotalSeconds)s $summary" | Add-Content $hist
Write-Output "STATUS=$status report=$report ($([int]$sw.Elapsed.TotalSeconds)s)"
switch ($status) { "OK" { exit 0 } "WARN" { exit 1 } "ALERT" { exit 2 } default { exit 3 } }
