# rxyy-mcp first-run install. Does not touch any existing rxyy-tools install.
param([switch]$Wizard)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$py = $null
foreach ($probe in @(
        @{ File = "py"; Args = @("-3.11") },
        @{ File = "python"; Args = @() }
    )) {
    try {
        $ver = & $probe.File @($probe.Args + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
        if ($ver -match "^3\.(1[1-9]|[2-9]\d)") {
            $py = @{ File = $probe.File; Args = $probe.Args }
            break
        }
    } catch { }
}
if (-not $py) { throw "Need Python 3.11+. Install it, then re-run install.ps1." }

Write-Host "Using $($py.File) $($py.Args -join ' ')"
& $py.File @($py.Args + @("-m", "pip", "install", "-e", "."))
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Start the hub:  rxyy-mcp hub --daemon"
Write-Host "  2. Optional desktop shell:  rxyy-mcp console"
Write-Host "  3. Open the web console:  http://127.0.0.1:38777/ui"
Write-Host "  4. Add this to your MCP client (Cursor ~/.cursor/mcp.json):"
Write-Host ""
Write-Host @'
{
  "mcpServers": {
    "rxyy-mcp": {
      "url": "http://127.0.0.1:39222/mcp"
    }
  }
}
'@
Write-Host ""
Write-Host "Phone share page (after hub is up): http://127.0.0.1:39080/"
