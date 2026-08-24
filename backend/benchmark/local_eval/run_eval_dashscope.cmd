@echo off
REM Full benchmark run against Alibaba DashScope (Qwen). Double-click to run.
REM
REM Uses the strongest generally-available Qwen models that fit the pipeline:
REM   planning / code generation : qwen3.8-max
REM   visual gate                : qwen3-vl-plus   (accepts images, writes code)
REM
REM Needs DASHSCOPE_API_KEY in backend\.env (gitignored) or in the environment.
REM No key is stored in this file.
REM
REM Override a model without editing anything:
REM   run_eval_dashscope.cmd -Model qwen3-coder-plus -VisionModel qwen3-vl-flash
REM Other useful flags: -N 3, -Difficulty moderate, -NoVisual, -Concurrency 3

setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Run-Eval.ps1" -Provider dashscope %*
endlocal
