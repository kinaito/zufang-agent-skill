@echo off
chcp 65001 >nul
title 佛山租房监控与地图管理系统

echo ==================================================
echo   🏠 佛山租房监控系统 - 智能找房服务启动中...  
echo ==================================================
echo.

cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 错误: 未检测到 Python 环境。
    echo 💡 请前往 https://www.python.org/ 下载并安装 Python (记得勾选 Add Python to PATH)。
    pause
    exit /b 1
)

echo 🔍 正在进行环境自检...
python agent_pack\cap.py doctor >nul 2>&1

echo 🚀 正在启动本地地图服务并打开浏览器...
python agent_pack\cap.py serve --open-browser

echo.
echo ==================================================
echo   ✅ 服务已成功运行！
echo   🌐 地图访问地址: http://127.0.0.1:18888/map.html
echo   💡 提示: 保持此窗口开启即可随时在浏览器中查看和筛选房源。
echo   🛑 若要停止服务，直接关闭此窗口即可。
echo ==================================================
echo.

pause
