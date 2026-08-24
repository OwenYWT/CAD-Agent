@echo off
REM Double-click launcher for Run-Eval.ps1.
REM
REM Windows opens a double-clicked .ps1 in an editor rather than running it, and
REM the default execution policy blocks unsigned scripts. This shim runs the
REM script in a scoped bypass that applies to this one process only -- it does
REM not change any machine or user execution-policy setting.

setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Run-Eval.ps1" %*
endlocal
