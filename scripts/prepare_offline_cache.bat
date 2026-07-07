@echo off
setlocal
cd /d %~dp0\..
set COMPOSE_DOCKER_CLI_BUILD=1
set DOCKER_BUILDKIT=1
python jarvis.py up --profile gemma4-mono
docker compose -f wsl/docker-compose.agents.yml --env-file wsl/.env pull --ignore-buildable
docker image ls
endlocal
