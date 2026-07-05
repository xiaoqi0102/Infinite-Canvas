param(
    [switch]$DryRun,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

$Script = Join-Path $PSScriptRoot "video_request_mode_patch.py"
$Args = @($Script, "--root", $Root)
if ($DryRun) { $Args += "--dry-run" }
if ($SkipChecks) { $Args += "--skip-checks" }

& $Python @Args
exit $LASTEXITCODE
