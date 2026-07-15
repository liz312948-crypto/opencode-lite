#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw "Verification failed: $Message"
    }
}

function Get-LowerHex {
    param([byte[]]$Bytes)
    return -join ($Bytes | ForEach-Object { $_.ToString("x2") })
}

function Get-StringSha256 {
    param([string]$Value)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return Get-LowerHex ($sha256.ComputeHash($encoding.GetBytes($Value)))
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-FileSha256 {
    param([string]$LiteralPath)
    $stream = [System.IO.File]::OpenRead($LiteralPath)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return Get-LowerHex ($sha256.ComputeHash($stream))
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Get-TreeFingerprint {
    param(
        [string]$Root,
        [string[]]$ExcludedDirectoryNames = @()
    )
    $rootPath = (Resolve-Path -LiteralPath $Root).Path
    $trimmedRoot = $rootPath.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $prefix = $trimmedRoot + [System.IO.Path]::DirectorySeparatorChar
    $records = New-Object System.Collections.Generic.List[string]
    $entries = @(Get-ChildItem -LiteralPath $rootPath -Force -Recurse | Sort-Object FullName)

    foreach ($entry in $entries) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Demo fixture contains a link or reparse point: $($entry.FullName)"
        }
        $relative = $entry.FullName.Substring($prefix.Length).Replace("\", "/")
        $parts = @($relative -split "/")
        $isExcluded = $false
        foreach ($part in $parts) {
            if ($ExcludedDirectoryNames -contains $part) {
                $isExcluded = $true
                break
            }
        }
        if ($isExcluded) {
            continue
        }
        if ($entry.PSIsContainer) {
            $records.Add("D|$relative")
        }
        else {
            $records.Add("F|$relative|$($entry.Length)|$(Get-FileSha256 $entry.FullName)")
        }
    }
    return Get-StringSha256 ($records -join "`n")
}

function Resolve-DemoPython {
    param([string]$RepositoryRoot)
    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:VIRTUAL_ENV)) {
        $candidates.Add((Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"))
        $candidates.Add((Join-Path $env:VIRTUAL_ENV "bin/python"))
    }
    $candidates.Add((Join-Path $RepositoryRoot ".venv\Scripts\python.exe"))
    $candidates.Add((Join-Path $RepositoryRoot ".venv/bin/python"))

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            & $candidate -c "import fastapi, pydantic, pytest, uvicorn" 2>$null
            if ($LASTEXITCODE -ne 0) {
                throw "Virtual environment exists but demo dependencies are missing: $candidate"
            }
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw (
        "No usable project virtual environment was found. Create .venv and run " +
        "python -m pip install -e `".[dev]`" first."
    )
}

function Invoke-OpenCodeApi {
    param(
        [ValidateSet("Get", "Post")]
        [string]$Method,
        [string]$Path,
        [object]$Body = $null
    )
    $request = @{
        Method = $Method
        Uri = "$script:ApiBase$Path"
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $request["ContentType"] = "application/json; charset=utf-8"
        $request["Body"] = $Body | ConvertTo-Json -Depth 20 -Compress
    }
    try {
        return Invoke-RestMethod @request
    }
    catch {
        $detail = $_.Exception.Message
        if ($null -ne $_.ErrorDetails -and
            -not [string]::IsNullOrWhiteSpace($_.ErrorDetails.Message)) {
            $detail = $_.ErrorDetails.Message
        }
        throw "API request failed: $Method $Path`n$detail"
    }
}

try {
    $baseUri = [System.Uri]$BaseUrl
}
catch {
    throw "BaseUrl is not a valid URI: $BaseUrl"
}
if ($baseUri.Scheme -notin @("http", "https") -or -not $baseUri.IsLoopback) {
    throw "Demo requests are restricted to a loopback HTTP(S) service."
}
$script:ApiBase = $BaseUrl.TrimEnd("/")
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$fixtureRoot = (Resolve-Path -LiteralPath (
    Join-Path $repositoryRoot "tests\fixtures\sample_repo"
)).Path
$sourceCalculator = Join-Path $fixtureRoot "calculator.py"
$ignoredNames = @(
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build"
)

Write-Step "Checking the project virtual environment"
$pythonPath = Resolve-DemoPython $repositoryRoot
Write-Host "Python: $pythonPath"
if (-not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    Write-Warning (
        "OPENAI_API_KEY is set in this shell. For a deterministic local demo, start " +
        "the API from a shell where it is unset."
    )
}

Write-Step "Checking the OpenCode-Lite service"
try {
    $openApi = Invoke-OpenCodeApi -Method Get -Path "/openapi.json"
}
catch {
    throw (
        "$($_.Exception.Message)`nStart the service in another terminal with:`n" +
        "python -m uvicorn repopilot_lite.main:app --host 127.0.0.1 " +
        "--port 8000 --workers 1"
    )
}
Assert-Condition ($openApi.info.title -ceq "OpenCode-Lite") "Unexpected API title."
Assert-Condition ($openApi.info.version -ceq "0.3.0-alpha") "Unexpected API version."
Write-Host "Service: $($openApi.info.title) $($openApi.info.version) at $script:ApiBase"

$sourceBefore = Get-TreeFingerprint $fixtureRoot
$baselineBefore = Get-TreeFingerprint $fixtureRoot $ignoredNames
$calculatorBefore = Get-FileSha256 $sourceCalculator
Assert-Condition (
    ([System.IO.File]::ReadAllText($sourceCalculator) -match "return left - right")
) "The fixture must start with its deliberately broken implementation."

Write-Step "Creating and analyzing a fixture task"
$created = Invoke-OpenCodeApi -Method Post -Path "/tasks" -Body @{
    repo_path = $fixtureRoot
    question = "Demonstrate rollback when a calculator patch fails its test."
    test_command = @("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")
    test_timeout_seconds = 30
}
Assert-Condition ($created.status -ceq "PENDING") "New task is not PENDING."
$taskId = [string]$created.task_id
Write-Host "Task: $taskId"

$analysis = Invoke-OpenCodeApi -Method Post -Path "/tasks/$taskId/run"
Assert-Condition ($analysis.status -ceq "SUCCESS") "Repository analysis did not succeed."

Write-Step "Submitting an applicable patch that will fail the fixture test"
$unifiedDiff = @'
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right + 1
'@
$unifiedDiff = $unifiedDiff.Replace("`r`n", "`n")
if (-not $unifiedDiff.EndsWith("`n")) {
    $unifiedDiff += "`n"
}
$proposal = Invoke-OpenCodeApi -Method Post -Path "/tasks/$taskId/patches" -Body @{
    unified_diff = $unifiedDiff
    reason = "Demonstrate verified rollback after a failing test."
    risk_level = "LOW"
    target_files = @("calculator.py")
    generated_by = "demo_rollback"
}
Assert-Condition ($proposal.validation_status -ceq "VALID") "Patch is not VALID."

Write-Step "Reviewing and approving the exact patch bytes"
$reviewed = Invoke-OpenCodeApi -Method Get -Path "/tasks/$taskId/diff"
Write-Host $reviewed.unified_diff
$reviewedHash = Get-StringSha256 ([string]$reviewed.unified_diff)
Assert-Condition ($reviewed.id -ceq $proposal.id) "Diff endpoint returned another patch."
Assert-Condition ($reviewedHash -ceq $reviewed.content_hash) "Local diff SHA-256 mismatch."
Write-Host "Patch ID: $($reviewed.id)"
Write-Host "SHA-256: $reviewedHash"

$approved = Invoke-OpenCodeApi -Method Post -Path "/tasks/$taskId/approve" -Body @{
    patch_id = $reviewed.id
    expected_content_hash = $reviewedHash
}
Assert-Condition ($approved.validation_status -ceq "APPROVED") "Patch was not approved."
Assert-Condition ($approved.approved_hash -ceq $reviewedHash) "Approved hash mismatch."

Write-Step "Executing the patch and observing verified rollback"
$execution = Invoke-OpenCodeApi -Method Post -Path "/tasks/$taskId/execute"
Assert-Condition ($execution.status -ceq "FAILED") "Task did not reach FAILED."
Assert-Condition ($execution.error_code -ceq "TESTS_FAILED") "Unexpected failure code."
$report = $execution.execution_report
Assert-Condition ($report.tests_passed -eq $false) "ExecutionReport says tests passed."
Assert-Condition ($report.rollback_triggered -eq $true) "Rollback was not triggered."
Assert-Condition ($report.rollback_succeeded -eq $true) "Rollback was not verified."
Assert-Condition ($report.source_unchanged -eq $true) "Source integrity check failed."
Assert-Condition ($report.failure_stage -ceq "testing") "Unexpected failure stage."
Assert-Condition ($report.final_status -ceq "FAILED") "Report status mismatch."
Assert-Condition (
    $report.final_manifest_hash -ceq $report.baseline_manifest_hash
) "Restored workspace manifest does not match the execution baseline."
Assert-Condition (
    $report.source_manifest_before_hash -ceq $report.source_manifest_after_hash
) "Source manifests differ."
Assert-Condition (
    @($report.test_results).Count -eq 1 -and @($report.test_results)[0].exit_code -ne 0
) "The fixture test did not record a non-zero exit."

$finalTask = Invoke-OpenCodeApi -Method Get -Path "/tasks/$taskId"
$logs = @(Invoke-OpenCodeApi -Method Get -Path "/tasks/$taskId/logs")
$workspaceRoot = [string]$finalTask.workspace_path
Assert-Condition (Test-Path -LiteralPath $workspaceRoot -PathType Container) (
    "Restored workspace is missing."
)
$workspaceAfter = Get-TreeFingerprint $workspaceRoot $ignoredNames
$workspaceCalculator = Join-Path $workspaceRoot "calculator.py"
Assert-Condition ($workspaceAfter -ceq $baselineBefore) (
    "Restored workspace file set or content differs from the fixture baseline."
)
Assert-Condition (
    (Get-FileSha256 $workspaceCalculator) -ceq $calculatorBefore
) "Workspace calculator.py was not restored."

Write-Step "Verifying the source fixture stayed byte-for-byte unchanged"
$sourceAfter = Get-TreeFingerprint $fixtureRoot
$calculatorAfter = Get-FileSha256 $sourceCalculator
Assert-Condition ($sourceAfter -ceq $sourceBefore) "Fixture source tree changed."
Assert-Condition ($calculatorAfter -ceq $calculatorBefore) "Source calculator.py changed."
Write-Host "Task status: $($finalTask.status)" -ForegroundColor Yellow
Write-Host "rollback_succeeded: $($finalTask.execution_report.rollback_succeeded)" `
    -ForegroundColor Green
Write-Host "Source fixture fingerprint: $sourceAfter" -ForegroundColor Green

Write-Step "Final Task"
$finalTask | ConvertTo-Json -Depth 20
Write-Step "ExecutionReport"
$finalTask.execution_report | ConvertTo-Json -Depth 20
Write-Step "Logs"
$logs | ConvertTo-Json -Depth 20

Write-Host "`nROLLBACK DEMO PASSED: failure was reported, workspace restored, source unchanged." `
    -ForegroundColor Green
