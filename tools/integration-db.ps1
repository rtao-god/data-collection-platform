param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'down', 'test', 'status')]
    [string]$Action = 'up',

    [int]$PostgresPort = 55432,

    [int]$StartupTimeoutSeconds = 120
)

# Isolated PostgreSQL/PostGIS for integration tests.
#
# Deliberately separate from the development stack: own Compose project, own container,
# own host port, tmpfs data. This script never touches deploy/compose/infrastructure.yaml.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'deploy/compose/integration.yaml'
$project = 'dcpf-integration'
$container = 'integration-postgres'
$database = 'collector_integration'
$userValue = 'collector_integration'
$password = 'collector_integration'

$env:DCPF_INTEGRATION_POSTGRES_PORT = "$PostgresPort"
$databaseUrl = "postgresql+psycopg://${userValue}:${password}@127.0.0.1:${PostgresPort}/${database}"

function Invoke-Compose {
    # Arguments are passed as an explicit array. `ValueFromRemainingArguments` would let
    # PowerShell bind `-d` to a common parameter instead of forwarding it, which silently
    # turned `up -d` into an attached `up` that streamed logs until the caller timed out.
    param([Parameter(Mandatory)][string[]]$ComposeArguments)

    & docker compose -p $project -f $composeFile @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Get-ContainerHealth {
    # `docker ps --format` returns one line per matching container, so a single string is
    # not enough to decide. Health is read as a discrete state: substring matching would
    # treat `unhealthy` as healthy and run migrations against a broken database.
    $lines = @(& docker ps -a --filter "name=$project" --format '{{.Names}}|{{.State}}|{{.Status}}')
    $match = $lines | Where-Object { $_ -like "$project-$container-*" } | Select-Object -First 1
    if (-not $match) { return 'missing' }

    $parts = $match -split '\|', 3
    $state = $parts[1]
    $status = $parts[2]

    if ($state -ne 'running') { return $state }
    if ($status -match '\(healthy\)') { return 'healthy' }
    if ($status -match '\(unhealthy\)') { return 'unhealthy' }
    if ($status -match '\(health: starting\)') { return 'starting' }
    return 'running'
}

function Wait-Healthy {
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        switch (Get-ContainerHealth) {
            'healthy' { return }
            'unhealthy' {
                throw "the integration database reported unhealthy; inspect: docker logs $project-$container-1"
            }
            'exited' {
                throw "the integration database container exited; inspect: docker logs $project-$container-1"
            }
        }
        Start-Sleep -Seconds 1
    }
    $health = Get-ContainerHealth
    throw "the integration database did not become healthy within ${StartupTimeoutSeconds}s (state: $health); inspect: docker logs $project-$container-1"
}

function Wait-DatabaseReady {
    # `pg_isready` succeeds as soon as PostgreSQL accepts connections, which happens before
    # the image's init scripts finish loading PostGIS and creating the application database.
    # Migrating inside that window fails intermittently, so readiness is proven by an actual
    # query against the real database instead of by the container's health report.
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    $lastError = 'no attempt was made'
    while ((Get-Date) -lt $deadline) {
        $result = & docker exec $project-$container-1 psql --username $userValue --dbname $database --no-psqlrc --quiet --tuples-only --command 'SELECT 1' 2>&1
        if ($LASTEXITCODE -eq 0 -and "$result".Trim() -eq '1') {
            return
        }
        $lastError = "$result".Trim()
        Start-Sleep -Seconds 1
    }
    throw "the integration database never accepted a query within ${StartupTimeoutSeconds}s: $lastError"
}

function Invoke-Migrations {
    $env:COLLECTOR_DATABASE_URL = $databaseUrl
    & uv run collection-migrate upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "migrations failed against the integration database"
    }
}

switch ($Action) {
    'up' {
        Invoke-Compose -ComposeArguments @('up', '--detach')
        Wait-Healthy
        Wait-DatabaseReady
        Invoke-Migrations
        Write-Output "COLLECTOR_DATABASE_URL=$databaseUrl"
    }
    'down' {
        Invoke-Compose -ComposeArguments @('down', '--volumes', '--remove-orphans')
        Write-Output 'integration database removed'
    }
    'status' {
        Invoke-Compose -ComposeArguments @('ps')
    }
    'test' {
        if ((Get-ContainerHealth) -ne 'healthy') {
            throw 'the integration database is not running; run: pwsh -NoProfile -File tools/integration-db.ps1 up'
        }
        Wait-DatabaseReady
        $env:COLLECTOR_DATABASE_URL = $databaseUrl
        & uv run pytest -m integration database/tests
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "integration tests failed with exit code $exitCode"
        }
    }
}
