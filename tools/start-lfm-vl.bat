@echo off
chcp 65001 >nul
title LFM2.5-VL-3B 本地视觉模型 (端口8080, OpenAI兼容API)
echo ================================================
echo   LiquidAI LFM2.5-VL-3B Q4_K_M  本地视觉服务
echo   API: http://127.0.0.1:8080/v1/chat/completions
echo   亮点: 视觉理解(mmproj已挂载) / 128K上下文 / CPU友好
echo   关闭: 直接关本窗口
echo ================================================
echo.
"D:\Programs\llama-bin\llama-server.exe" -m "D:\AI\models\lfm2.5-vl-3b-q4_k_m.gguf" --mmproj "D:\AI\models\mmproj-lfm2.5-vl-3b-bf16.gguf" --host 127.0.0.1 --port 8080 -c 8192 -t 8 -ngl 0
pause
