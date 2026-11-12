[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet(
        'verify',
        'contracts',
        'format',
        'lint',
        'types',
        'architecture',
        'tests',
        'campaign')]
    [string]$Command
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepositoryRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Python {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & uv run @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "CHECK_STEP_FAILED: uv run $($Arguments -join ' '); exit code $LASTEXITCODE"
    }
}

function Invoke-Contracts {
    Invoke-Python @('tools/contract_generation/generate.py', '--check')
    Invoke-Python @('tools/review_contract_generation/generate.py', '--check')
    Invoke-Python @('tools/control_api_contract_generation/generate.py', '--check')
}

function Invoke-Format {
    Invoke-Python @('-m', 'ruff', 'format', '--check', '.')
}

function Invoke-Lint {
    Invoke-Python @('-m', 'ruff', 'check', '.')
}

function Invoke-Types {
    Invoke-Python @('-m', 'mypy')
}

function Invoke-Architecture {
    Invoke-Python @('tools/architecture_checks/check_dependencies.py')
    Invoke-Python @('tools/architecture_checks/check_workflows.py')
}

function Invoke-Tests {
    Invoke-Python @('-m', 'pytest', '-m', 'not integration')
}

function Invoke-Campaign {
    Invoke-Python @('collector', 'config', 'validate', 'berlin_recording_services')
}

Push-Location $RepositoryRoot
try {
    switch ($Command) {
        'contracts' { Invoke-Contracts }
        'format' { Invoke-Format }
        'lint' { Invoke-Lint }
        'types' { Invoke-Types }
        'architecture' { Invoke-Architecture }
        'tests' { Invoke-Tests }
        'campaign' { Invoke-Campaign }
        'verify' {
            Invoke-Contracts
            Invoke-Format
            Invoke-Lint
            Invoke-Types
            Invoke-Architecture
            Invoke-Tests
            Invoke-Campaign
        }
    }
}
finally {
    Pop-Location
}
