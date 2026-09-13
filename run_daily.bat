@echo off
rem 面经流水线每日运行入口(供手动执行或 Windows 计划任务调用)
chcp 65001 >nul
cd /d "%~dp0"

echo [%date% %time%] 开始运行面经流水线 >> logs\run_daily.log 2>&1
python -m pipeline run --config config.yaml >> logs\run_daily.log 2>&1
echo [%date% %time%] 运行结束,退出码 %errorlevel% >> logs\run_daily.log 2>&1

rem 手动双击运行时给出提示;计划任务调用时无窗口不受影响
if "%1" neq "scheduled" (
    if %errorlevel% equ 0 (
        echo 运行完成,输出见 output\ 目录,日志见 logs\pipeline.log
    ) else (
        echo 运行失败,请查看 logs\pipeline.log
    )
    pause
)
