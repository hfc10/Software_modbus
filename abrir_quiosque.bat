@echo off
rem Abre o painel em tela cheia (modo quiosque) no modo toque, pra tela do HMI.
rem Espera uns segundos pro servidor (iniciar_painel.bat) terminar de subir.
rem Se mudou "porta_http" no config.json, troque 5000 abaixo pela mesma porta.
rem Pra sair do modo quiosque do Edge: Alt+F4.
rem Se preferir o Chrome, troque a linha do msedge por:
rem   start "" chrome --kiosk "http://localhost:5000/?quiosque=1"
timeout /t 5 /nobreak >nul
start "" msedge --kiosk "http://localhost:5000/?quiosque=1" --edge-kiosk-type=fullscreen
