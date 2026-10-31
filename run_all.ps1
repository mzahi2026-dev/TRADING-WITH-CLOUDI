$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$jobs = @(
  @{t="COLLECTOR"; f="main.py"},
  @{t="SCHEDULER"; f="scanner_main.py"},
  @{t="DASHBOARD"; f="app_ui.py"},
  @{t="TRADER"; f="trade_main.py"}
)
foreach ($j in $jobs) {
  $cmd = "`$host.UI.RawUI.WindowTitle='$($j.t)'; Set-Location '$root'; while(`$true){ & '$py' $($j.f); Write-Host 'Process stopped. 5s por restart...' -ForegroundColor Yellow; Start-Sleep 5 }"
  Start-Process powershell -ArgumentList "-NoExit","-Command",$cmd
  Start-Sleep 2
}