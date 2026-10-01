@echo off
rem Sobe o painel do secador e sobe de novo sozinho se ele cair.
rem Pra parar de vez: feche esta janela (ou Ctrl+C).
rem Argumentos extras vao direto pro app.py, ex: iniciar_painel.bat --simular
rem Pra iniciar junto com o Windows: Win+R, digite shell:startup e coloque um atalho
rem pra este arquivo dentro da pasta que abrir.
title Painel Secador
cd /d "%~dp0"

:inicio
"%~dp0venv\Scripts\python.exe" "%~dp0app.py" %*
echo.
echo [%date% %time%] O painel parou. Reiniciando em 5 segundos... (feche esta janela pra parar)
timeout /t 5 /nobreak >nul
goto inicio
