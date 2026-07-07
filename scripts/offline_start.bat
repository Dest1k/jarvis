@echo off
setlocal
cd /d %~dp0\..
set JARVIS_PULL_POLICY=never
set COMPOSE_DOCKER_CLI_BUILD=1
set DOCKER_BUILDKIT=1
python jarvis.py up --profile gemma4-mono
endlocal
