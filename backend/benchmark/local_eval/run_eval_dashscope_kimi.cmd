@echo off
REM Full benchmark run on DashScope with Moonshot Kimi as the LANGUAGE model.
REM Double-click to run.
REM
REM   planning / code generation : kimi-k3        (most advanced Kimi on DashScope)
REM   visual gate                : qwen3-vl-plus  (Kimi has no vision variant there)
REM
REM Both models are served through the one DashScope endpoint, so this needs only
REM DASHSCOPE_API_KEY in backend\.env (gitignored) or in the environment.
REM No key is stored in this file.
REM
REM Compare against the Qwen-language run:
REM   run_eval_dashscope.cmd        -> qwen3-coder-plus + qwen3-vl-plus
REM   run_eval_dashscope_kimi.cmd   -> kimi-k3         + qwen3-vl-plus
REM
REM Override either model:
REM   run_eval_dashscope_kimi.cmd -Model kimi-k2.7-code
REM Other useful flags: -N 3, -Difficulty moderate, -NoVisual, -Concurrency 3

setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Run-Eval.ps1" -Provider dashscope-kimi %*
endlocal
