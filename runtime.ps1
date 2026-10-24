function Get-WorkflowHome {
    $taskHome = if ($env:SWE_WORKFLOW_HOME) { $env:SWE_WORKFLOW_HOME } else {
        Join-Path (Split-Path $PSScriptRoot -Parent) ((Split-Path $PSScriptRoot -Leaf) + '-local')
    }
    $taskHome = [System.IO.Path]::GetFullPath($taskHome)
    $env:SWE_WORKFLOW_HOME = $taskHome
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $taskHome '.cache/browsers'
    $env:PIP_CACHE_DIR = Join-Path $taskHome '.cache/pip'
    $env:npm_config_cache = Join-Path $taskHome '.cache/npm'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    return $taskHome
}
