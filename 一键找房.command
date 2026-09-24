#!/bin/bash
# ==============================================================================
# 佛山租房监控与地图管理系统 - 一键启动器 (macOS 专属)
# 双击此脚本即可全自动检查环境、启动本地可视化房源服务并打开浏览器
# ==============================================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "=================================================="
echo "  🏠 佛山租房监控系统 - 智能找房服务启动中...  "
echo "=================================================="
echo ""

# 1. 检查 Python3
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未检测到 Python 3 环境。"
    echo "💡 请前往 https://www.python.org/ 下载并安装 Python 3。"
    read -p "按回车键退出..."
    exit 1
fi

# 2. 检查并清理端口占用
PORT=18888
PID=$(lsof -ti:$PORT 2>/dev/null)
if [ -n "$PID" ]; then
    echo "🔄 正在释放被占用的端口 $PORT (PID: $PID)..."
    kill -9 $PID 2>/dev/null
    sleep 1
fi

# 3. 运行环境健康体检
echo "🔍 正在进行环境自检..."
python3 agent_pack/cap.py doctor > /dev/null 2>&1

# 4. 启动本地地图服务并自动唤起默认浏览器
echo "🚀 正在启动本地地图服务并打开浏览器..."
python3 agent_pack/cap.py serve --open-browser

echo ""
echo "=================================================="
echo "  ✅ 服务已成功运行！"
echo "  🌐 地图访问地址: http://127.0.0.1:18888/map.html"
echo "  💡 提示: 保持此窗口开启即可随时在浏览器中查看和筛选房源。"
echo "  🛑 若要停止服务，直接关闭此终端窗口即可。"
echo "=================================================="
echo ""

# 保持窗口驻留
while true; do
    sleep 3600
done
