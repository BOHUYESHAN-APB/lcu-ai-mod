param(
    [ValidateSet('Status', 'Discover', 'Raw', 'Skill', 'Preset')]
    [string]$Mode = 'Status',
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [string]$ApiToken = $env:SDK_API_TOKEN,
    [string]$Command,
    [string]$SkillId,
    [string]$PresetId,
    [string]$InputJson = '{}',
    [string]$Owner = 'powershell-headed-test',
    [int]$TimeoutSeconds = 180,
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$base = $BaseUrl.TrimEnd('/')
$baseUri = $null
if (-not [Uri]::TryCreate($base, [UriKind]::Absolute, [ref]$baseUri)) {
    throw '-BaseUrl must be an absolute HTTP(S) URL.'
}
if ($baseUri.Scheme -notin @('http', 'https')) { throw '-BaseUrl must use HTTP or HTTPS.' }
$loopback = $baseUri.Host -in @('127.0.0.1', 'localhost', '::1')
if (-not $loopback -and $baseUri.Scheme -ne 'https') {
    throw 'Remote LCU backends require HTTPS; refusing to send credentials over plaintext HTTP.'
}
$headers = @{}
if ($ApiToken) { $headers.Authorization = "Bearer $ApiToken" }
$transcript = [System.Collections.Generic.List[object]]::new()

function Invoke-Lcu {
    param([string]$Path, [string]$Method = 'GET', [object]$Body)
    $request = @{ Uri = "$base$Path"; Method = $Method; Headers = $headers }
    if ($null -ne $Body) {
        $request.ContentType = 'application/json'
        $request.Body = $Body | ConvertTo-Json -Depth 20 -Compress
    }
    $result = Invoke-RestMethod @request
    $loggedResult = $result
    if ($Path -like '/api/v2/control/leases*') {
        $leaseView = if ($null -ne $result.lease) { $result.lease } else { $result }
        $loggedResult = [ordered]@{
            id = $leaseView.id
            owner = $leaseView.owner
            mode = $leaseView.mode
            status = $leaseView.status
            expires_at = $leaseView.expires_at
            fencing_token = '[redacted]'
        }
    }
    $transcript.Add([ordered]@{
        at = [DateTimeOffset]::UtcNow.ToString('o')
        method = $Method
        path = $Path
        result = $loggedResult
    })
    return $result
}

function ConvertTo-Object {
    param([string]$Json)
    if ([string]::IsNullOrWhiteSpace($Json)) { return @{} }
    return $Json | ConvertFrom-Json -AsHashtable
}

function Assert-BodyReady {
    param([object]$Status)
    $bodyState = $Status.session.body
    if (-not $bodyState.connected) { throw 'Companion body is offline.' }
    if (-not $bodyState.armed) { throw 'Companion body is disarmed. Press F12 in Minecraft.' }
    if ($bodyState.stale) { throw "Companion body telemetry is stale (age=$($bodyState.state_age_seconds))." }
}

function Wait-BodyRequest {
    param([string]$RequestId)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $request = Invoke-Lcu "/api/v2/body-requests/$([uri]::EscapeDataString($RequestId))"
        Write-Host ("{0,-10} {1,6:P0} {2}" -f $request.status, ($request.progress ?? 0), $request.detail)
        if ($request.terminal) { return $request }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "Timed out waiting for body request $RequestId"
}

function Wait-Run {
    param([string]$RunId, [object]$Lease)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $nextHeartbeat = [DateTimeOffset]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Seconds 1
        if ([DateTimeOffset]::UtcNow -ge $nextHeartbeat) {
            $null = Invoke-Lcu "/api/v2/control/leases/$($Lease.id)/heartbeat" 'POST' @{
                fencing_token = $Lease.fencing_token
                ttl_seconds = 60
            }
            $nextHeartbeat = [DateTimeOffset]::UtcNow.AddSeconds(20)
        }
        $run = Invoke-Lcu "/api/v2/runs/$([uri]::EscapeDataString($RunId))"
        Write-Host ("{0,-10} {1,6:P0} {2} {3}" -f $run.status, $run.progress, $run.detail, $run.error)
        if ($run.status -in @('succeeded', 'failed', 'cancelled', 'unknown')) { return $run }
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "Timed out waiting for run $RunId"
}

$lease = $null
$terminal = $null
$releaseFailed = $false
$exportFailed = $false
try {
    $status = Invoke-Lcu '/api/status'
    Write-Host "Backend running: $($status.running)"
    Write-Host "Body connected=$($status.session.body.connected) armed=$($status.session.body.armed) stale=$($status.session.body.stale)"

    switch ($Mode) {
        'Status' {
            $terminal = $status
        }
        'Discover' {
            $skills = Invoke-Lcu '/api/v2/skills'
            $presets = Invoke-Lcu '/api/v2/task-presets'
            $terminal = [ordered]@{ status = $status; skills = $skills.skills; presets = $presets.presets }
            $skills.skills | Format-Table id, available, category, command
            $presets.presets | Format-Table id, available, category, title
        }
        'Raw' {
            Assert-BodyReady $status
            if ([string]::IsNullOrWhiteSpace($Command)) { throw '-Command is required for Raw mode.' }
            $accepted = Invoke-Lcu '/api/sdk/command' 'POST' @{
                command = $Command
                args = ConvertTo-Object $InputJson
            }
            $terminal = Wait-BodyRequest $accepted.request_id
        }
        { $_ -in @('Skill', 'Preset') } {
            Assert-BodyReady $status
            if ($Mode -eq 'Skill' -and [string]::IsNullOrWhiteSpace($SkillId)) {
                throw '-SkillId is required for Skill mode.'
            }
            if ($Mode -eq 'Preset' -and [string]::IsNullOrWhiteSpace($PresetId)) {
                throw '-PresetId is required for Preset mode.'
            }
            $lease = (Invoke-Lcu '/api/v2/control/leases' 'POST' @{
                owner = $Owner
                mode = 'external'
                ttl_seconds = 60
            }).lease
            $leaseFields = @{ lease_id = $lease.id; fencing_token = $lease.fencing_token }
            if ($Mode -eq 'Skill') {
                $path = "/api/v2/skills/$([uri]::EscapeDataString($SkillId))/runs"
                $run = Invoke-Lcu $path 'POST' @{
                    input = ConvertTo-Object $InputJson
                    lease_id = $leaseFields.lease_id
                    fencing_token = $leaseFields.fencing_token
                }
            } else {
                $path = "/api/v2/task-presets/$([uri]::EscapeDataString($PresetId))/runs"
                $run = Invoke-Lcu $path 'POST' @{
                    parameters = ConvertTo-Object $InputJson
                    lease_id = $leaseFields.lease_id
                    fencing_token = $leaseFields.fencing_token
                }
            }
            $terminal = Wait-Run $run.id $lease
        }
    }
} finally {
    if ($null -ne $lease) {
        try {
            $null = Invoke-Lcu "/api/v2/control/leases/$($lease.id)/release" 'POST' @{
                fencing_token = $lease.fencing_token
            }
        } catch {
            $releaseFailed = $true
            Write-Warning "Failed to release control lease: $($_.Exception.Message)"
        }
    }
    if ($OutputPath) {
        try {
            [ordered]@{ mode = $Mode; terminal = $terminal; requests = $transcript } |
                ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $OutputPath -Encoding utf8
        } catch {
            $exportFailed = $true
            Write-Warning "Failed to export diagnostics: $($_.Exception.Message)"
        }
    }
}

if ($releaseFailed) { exit 3 }
if ($exportFailed) { exit 4 }
if ($null -ne $terminal -and $terminal.status -in @('failed', 'cancelled', 'unknown')) {
    exit 2
}
