@echo off
REM local-qwen.bat —— 启动本地 llama-server（qwen2.5-3b Q4_K_M，OpenAI 兼容 API）
REM 用法：local-qwen.bat [端口] [--bg]（默认 8080；--bg 后台启动无窗口）
REM agt models.json 的 local-qwen 条目指向 http://127.0.0.1:8080/v1
setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8080
if "%PORT%"=="--bg" (set PORT=8080 & set BG=1) else if "%2"=="--bg" set BG=1

set SERVER=D:\Programs\llama\bin\llama-server.exe
set MODEL=E:\AI\models\qwen2.5-3b-instruct-q4_k_m.gguf

if not exist "%SERVER%" (echo [错误] 找不到 %SERVER% & exit /b 1)
if not exist "%MODEL%" (echo [错误] 找不到模型 %MODEL% & exit /b 1)

echo [local-qwen] 端口 %PORT%  模型 qwen2.5-3b-instruct-q4_k_m (CPU)
echo [local-qwen] API: http://127.0.0.1:%PORT%/v1  (Ctrl+C 停止)

if defined BG (
  start "" /B "%SERVER%" -m "%MODEL%" --host 127.0.0.1 --port %PORT% -c 4096 -t 6 >nul 2>&1
) else (
  "%SERVER%" -m "%MODEL%" --host 127.0.0.1 --port %PORT% -c 4096 -t 6
)
