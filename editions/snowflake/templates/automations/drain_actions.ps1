<#
.SYNOPSIS
  Wapsi ACTIONS drain runner (reference - validate in a CoCo session).

.DESCRIPTION
  Every -IntervalSeconds, counts PENDING Jira/Slack actions in WAPSI.CASEMGMT.ACTIONS with one cheap
  `snow sql` query and calls CoCo headless (`cortex -p`) only when there is work, so an idle queue costs
  no LLM tokens. CoCo uses the Jira and Slack MCP servers configured in ~/.snowflake/cortex/mcp.json and the
  instructions in actions-drain.md next to this script.

  Needs: Snowflake CLI (`snow`), CoCo CLI (`cortex`), a connection named -Connection whose default role can
  assume WAPSI_ACTIONS_RUNNER, and the WAPSI_JIRA_* / WAPSI_SLACK_* environment variables used by mcp.json.

.EXAMPLE
  pwsh -File editions/snowflake/templates/automations/drain_actions.ps1 -Connection wapsi
.EXAMPLE
  pwsh -File editions/snowflake/templates/automations/drain_actions.ps1 -Once
#>
param(
    [string]$Connection = "wapsi",
    [int]$IntervalSeconds = 60,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$promptFile = "editions/snowflake/templates/automations/actions-drain.md"
$prompt = "Follow $promptFile exactly: drain pending WAPSI Jira and Slack actions, then print the one-line summary."
$countSql = "SELECT COUNT(*) AS N FROM WAPSI.CASEMGMT.ACTIONS WHERE STATUS = 'PENDING' AND KIND IN ('JIRA_TICKET', 'SLACK_NOTICE')"
$allowed = "snowflake_sql_execute,read,mcp__jira__*,mcp__slack__*"

if (-not (Get-Command snow -ErrorAction SilentlyContinue)) { throw "Snowflake CLI 'snow' not found on PATH" }
if (-not (Get-Command cortex -ErrorAction SilentlyContinue)) { throw "CoCo CLI 'cortex' not found on PATH" }

do {
    $pending = 0
    try {
        $raw = snow sql -c $Connection --role WAPSI_ACTIONS_RUNNER -q $countSql --format JSON | Out-String
        $rows = @($raw | ConvertFrom-Json)
        if ($rows.Count -gt 0) {
            $first = $rows[0]
            if ($first -is [array]) { $first = $first[0] }   # multi-statement shape: [[{...}]]
            $pending = [int]$first.N
        }
    }
    catch {
        Write-Warning "$(Get-Date -Format s) pending count failed: $($_.Exception.Message)"
    }

    if ($pending -gt 0) {
        Write-Host "$(Get-Date -Format s) $pending pending Jira/Slack action(s) -> cortex"
        Push-Location $repo
        try {
            cortex -c $Connection -w $repo -p $prompt --allowed-tools $allowed
        }
        finally {
            Pop-Location
        }
    }
    else {
        Write-Host "$(Get-Date -Format s) nothing pending"
    }

    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
} while ($true)
