<#
.SYNOPSIS
    Windows shim for the Makefile targets.

.DESCRIPTION
    A stock Windows install has no `make`, so this script exposes the same target
    names and runs the same commands. The Makefile remains canonical - CI and any
    Linux/macOS/WSL checkout use it directly. Keep the two in step.

.EXAMPLE
    ./make.ps1 test
    ./make.ps1 sim -N 5000
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Target = 'help',

    # Vehicle count for the `sim` and `seed` targets.
    [int]$N = 2000,
    [int]$Vehicles = 100000
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Coverage gate scope matches the Makefile (ruling G7).
$CovMin = 80

function Invoke-Step {
    param([string]$Description, [scriptblock]$Action)
    Write-Host "==> $Description" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw @'
Docker is not available on PATH.

This machine is Windows Home, so Docker Desktop needs the WSL2 backend.
See docs/PROGRESS.md (blocker B1) for the fix, then re-run this target.
'@
    }
}

switch ($Target.ToLowerInvariant()) {

    'help' {
        Write-Host 'Targets:' -ForegroundColor Green
        @(
            @{ n = 'install';          d = 'Sync the pinned Python workspace and web dependencies' }
            @{ n = 'up';               d = 'Start the full local stack (needs Docker)' }
            @{ n = 'down';             d = 'Stop the stack and remove volumes' }
            @{ n = 'logs';             d = 'Tail logs from every service' }
            @{ n = 'lint';             d = 'ruff check, ruff format --check, mypy' }
            @{ n = 'fmt';              d = 'Auto-format Python sources' }
            @{ n = 'test';             d = "Unit tests with the $CovMin% coverage gate" }
            @{ n = 'test-unit';        d = 'Unit tests only, no infrastructure needed' }
            @{ n = 'test-integration'; d = 'Integration tests (needs Docker)' }
            @{ n = 'verify';           d = 'lint + test, everything CI enforces' }
            @{ n = 'sim';              d = 'Run the simulator: ./make.ps1 sim -N 5000' }
            @{ n = 'seed';             d = 'Seed the reference dataset' }
        ) | ForEach-Object { Write-Host ("  {0,-18} {1}" -f $_.n, $_.d) }
    }

    'install' {
        Invoke-Step 'uv sync' { uv sync --all-packages }
        if (Test-Path 'web/package.json') {
            Invoke-Step 'npm ci' { Push-Location web; npm ci; Pop-Location }
        }
    }

    'up' {
        Test-Docker
        if (-not (Test-Path '.env')) {
            Copy-Item '.env.example' '.env'
            Write-Host 'Created .env from .env.example - review it before running.' -ForegroundColor Yellow
        }
        Invoke-Step 'docker compose up' { docker compose --profile core up -d --wait }
        Invoke-Step 'create topics'     { uv run python -m processor.admin create-topics }
        Invoke-Step 'migrate'           { uv run python -m batch.migrate up }
        Write-Host 'Stack up. API http://localhost:8000/docs  Web http://localhost:5173' -ForegroundColor Green
    }

    'down' {
        Test-Docker
        docker compose --profile core --profile obs --profile load down -v --remove-orphans
    }

    'logs' { Test-Docker; docker compose logs -f --tail=100 }
    'ps'   { Test-Docker; docker compose ps }

    'lint' {
        Invoke-Step 'ruff check'        { uv run ruff check . }
        Invoke-Step 'ruff format check' { uv run ruff format --check . }
        Invoke-Step 'mypy'              { uv run mypy }
    }

    'fmt' {
        Invoke-Step 'ruff check --fix' { uv run ruff check --fix . }
        Invoke-Step 'ruff format'      { uv run ruff format . }
    }

    'test' {
        New-Item -ItemType Directory -Force -Path 'docs/evidence/coverage' | Out-Null
        Invoke-Step 'pytest' {
            uv run pytest -m "not integration" --cov --cov-report=term-missing `
                --cov-report=xml:docs/evidence/coverage/coverage.xml --cov-fail-under=$CovMin
        }
    }

    'test-unit' {
        Invoke-Step 'pytest' { uv run pytest -m "not integration" --cov --cov-fail-under=$CovMin }
    }

    'test-integration' {
        Test-Docker
        Invoke-Step 'pytest -m integration' { uv run pytest -m integration }
    }

    'verify' {
        & $PSCommandPath 'lint'
        & $PSCommandPath 'test'
    }

    'sim'  { Invoke-Step "simulator ($N vehicles)" { uv run python -m simulator --vehicles $N } }
    'seed' { Invoke-Step "seed ($Vehicles vehicles)" { uv run python -m batch.seed --vehicles $Vehicles } }

    default {
        Write-Error "Unknown target '$Target'. Run './make.ps1 help' for the list."
        exit 1
    }
}
