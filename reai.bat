@echo off
setlocal
cd /d "%~dp0"
title REAI - comandos simples

if /i "%~1"=="processar" goto processar
if /i "%~1"=="simular" goto simular
if /i "%~1"=="prever" goto prever
if /i "%~1"=="limpar" goto limpar
if /i "%~1"=="site" goto site
if /i "%~1"=="opcoes" goto opcoes
if /i "%~1"=="ajuda" goto ajuda
if not "%~1"=="" goto ajuda

:menu
echo.
echo  REAI - o que queres fazer?
echo.
echo  [1] Processar um video
echo  [2] Prever os frames existentes
echo  [3] Limpar frames e ficheiros gerados
echo  [4] Abrir o site local
echo  [5] Ver opcoes avancadas
echo  [Q] Sair
echo.
choice /c 12345Q /n /m "Escolhe uma opcao: "
if errorlevel 6 exit /b 0
if errorlevel 5 goto opcoes
if errorlevel 4 goto site
if errorlevel 3 goto limpar
if errorlevel 2 goto prever
if errorlevel 1 goto processar

:processar
set "VIDEO=%~2"
if "%VIDEO%"=="" set /p "VIDEO=Indica o caminho do video (ex.: Videos\seu_video.mp4): "
if not exist "%VIDEO%" (
  echo [ERRO] Nao encontrei o video: "%VIDEO%"
  exit /b 1
)
where python >nul 2>nul || (echo [ERRO] Python nao esta instalado ou nao esta no PATH.& exit /b 1)
where ffmpeg >nul 2>nul || (echo [ERRO] FFmpeg nao esta no PATH.& exit /b 1)
where ffprobe >nul 2>nul || (echo [ERRO] FFprobe nao esta no PATH.& exit /b 1)
set "FPS=%~3"
if "%FPS%"=="" set "FPS=30"
set "QUALITY=%~4"
if "%QUALITY%"=="" set "QUALITY=70"
echo.
echo A processar "%VIDEO%" ^(FPS %FPS%, qualidade %QUALITY%^)...
python cli\process_video.py --input "%VIDEO%" --output assets\frames --fps "%FPS%" --quality "%QUALITY%"
exit /b %errorlevel%

:simular
set "VIDEO=%~2"
if "%VIDEO%"=="" (
  echo Uso: reai.bat simular "Videos\seu_video.mp4"
  exit /b 2
)
python cli\process_video.py --input "%VIDEO%" --output assets\frames --dry-run
exit /b %errorlevel%

:prever
python cli\process_video.py --preview --output assets\frames
if errorlevel 1 exit /b %errorlevel%
for %%I in ("assets\frames\preview.html") do start "" "%%~fI"
exit /b 0

:limpar
python cli\process_video.py --clean-frames --output assets\frames
exit /b %errorlevel%

:site
echo O site local estara em http://localhost:8000
echo Para parar o servidor, pressiona Ctrl+C.
start "" http://localhost:8000
python -m http.server 8000
exit /b %errorlevel%

:opcoes
python cli\process_video.py --help
exit /b %errorlevel%

:ajuda
echo.
echo Uso: reai.bat [comando] [argumentos]
echo.
echo   reai.bat processar "Videos\seu_video.mp4" [fps] [qualidade]
echo   reai.bat simular "Videos\seu_video.mp4"
echo   reai.bat prever
echo   reai.bat limpar
echo   reai.bat site
echo   reai.bat opcoes
echo.
echo Sem argumentos, abre um menu interativo.
echo Para usar todas as opcoes avancadas, executa:
echo   python cli\process_video.py --input "Videos\seu_video.mp4" [opcoes]
exit /b 0
