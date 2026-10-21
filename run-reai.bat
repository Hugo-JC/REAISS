@echo off
setlocal
cd /d "%~dp0"
title REAI Scroll Sequence Engine - Nulled_

echo ================================================
echo   REAI Scroll Sequence Engine ^| Nulled_
echo ================================================
echo.

if "%~1"=="" (
  echo Uso: arrasta um video para este ficheiro BAT ou executa:
  echo   run-reai.bat "Videos\seu_video.mp4"
  echo.
  set /p VIDEO=Ou escreve o caminho do video: 
) else (
  set "VIDEO=%~1"
)

if not exist "%VIDEO%" (
  echo [ERRO] Nao encontrei o video: "%VIDEO%"
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul || (echo [ERRO] FFmpeg nao esta no PATH. & pause & exit /b 1)
where ffprobe >nul 2>nul || (echo [ERRO] FFprobe nao esta no PATH. & pause & exit /b 1)
python --version >nul 2>nul || (echo [ERRO] Python nao esta instalado ou nao esta no PATH. & pause & exit /b 1)

echo [1/2] A processar video...
python cli\process_video.py --input "%VIDEO%" --output assets\frames --fps 30 --quality 70
if errorlevel 1 (
  echo.
  echo [ERRO] O processamento falhou. Verifica as mensagens acima.
  pause
  exit /b 1
)

echo.
echo [2/2] A iniciar servidor local em http://localhost:8000
echo Para parar o servidor, pressiona Ctrl+C.
start "REAI Preview" http://localhost:8000
python -m http.server 8000
endlocal
