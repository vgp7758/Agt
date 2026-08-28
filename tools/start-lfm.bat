@echo off
chcp 65001 >nul
title LFM2.5-2.6B 本地大模型 (端口8081, OpenAI兼容API)
echo ================================================
echo   LiquidAI LFM2.5-2.6B QAD-Q4_0  本地推理服务
echo   API: http://127.0.0.1:8081/v1/chat/completions
echo   亮点: 128K上下文 / 原生工具调用 / 思考分离(deepseek格式) / CPU友好
echo   关闭: 直接关本窗口
echo ================================================
echo.
"D:\Programs\llama-bin\llama-server.exe" -m "D:\AI\models\lfm2.5-2.6b-qad-q4_0.gguf" --reasoning-format deepseek --host 127.0.0.1 --port 8081 -c 8192 -t 8 -ngl 0
pause
