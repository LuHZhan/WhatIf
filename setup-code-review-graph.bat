@echo off
chcp 65001 >nul
echo ========================================
echo  安装 code-review-graph 代码知识图谱
echo ========================================
echo.

echo [1/3] 安装 code-review-graph...
pip install code-review-graph
if %errorlevel% neq 0 (
    echo 安装失败，请检查 Python 环境
    pause
    exit /b 1
)
echo.

echo [2/3] 配置环境（MCP 接入）...
code-review-graph install
if %errorlevel% neq 0 (
    echo 配置失败
    pause
    exit /b 1
)
echo.

echo [3/3] 构建代码库知识图谱...
code-review-graph build
if %errorlevel% neq 0 (
    echo 构建失败
    pause
    exit /b 1
)
echo.

echo ========================================
echo  完成！知识图谱已就绪 input：为这个代码审查构建代码图
echo ========================================
pause
