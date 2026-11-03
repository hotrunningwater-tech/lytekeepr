# Installs the LyteKeepr LinkedIn helper on Windows, in the person's own folder (no administrator rights).
param([string]$Code)
$ErrorActionPreference = "Stop"
$Address = "__ADDRESS__"
$Package = "__PACKAGE__"
$Dir = Join-Path $env:USERPROFILE ".lytekeepr-linkedin"
if (-not $Code) { Write-Host "The connection code is missing: copy the whole line from LyteKeepr (LinkedIn tab)."; return }
New-Item -ItemType Directory -Force -Path (Join-Path $Dir "bin") | Out-Null

Write-Host "1 of 4: getting the helper..."
$found = Get-Command uv -ErrorAction SilentlyContinue
if ($found) { $Uv = $found.Source } else {
  $env:UV_UNMANAGED_INSTALL = (Join-Path $Dir "bin")
  Invoke-RestMethod "https://astral.sh/uv/__UV_VERSION__/install.ps1" | Invoke-Expression
  $Uv = Join-Path $Dir "bin\uv.exe"
}
$Agent = Join-Path $Dir "agent.py"
Invoke-WebRequest -UseBasicParsing "$Address/linkedin-helper/agent.py" -OutFile $Agent

Write-Host "2 of 4: connecting to your workspace..."
& $Uv run --quiet --script $Agent pair $Code $Address
if ($LASTEXITCODE -ne 0) { return }

Write-Host "3 of 4: a browser window opens. Log in to LinkedIn there, then this continues by itself."
$ErrorActionPreference = "Continue"
& $Uv tool run --quiet --from $Package linkedin-mcp profile create 2>$null | Out-Null
$ErrorActionPreference = "Stop"
& $Uv tool run --quiet --from $Package linkedin-mcp setup | Out-Null
& $Uv tool run --quiet --from $Package linkedin-mcp login | Out-Null

Write-Host "4 of 4: starting the helper..."
$Quiet = Join-Path $Dir "run.vbs"
Set-Content -Path $Quiet -Value ('CreateObject("WScript.Shell").Run """' + $Uv + '"" run --quiet --script ""' + $Agent + '"" tick", 0, False')
schtasks /Create /F /TN "LyteKeepr LinkedIn" /SC MINUTE /MO 15 /TR "wscript.exe `"$Quiet`"" | Out-Null
schtasks /Run /TN "LyteKeepr LinkedIn" | Out-Null
Write-Host "Done. Your LinkedIn messages will appear in LyteKeepr. You can close this window."
