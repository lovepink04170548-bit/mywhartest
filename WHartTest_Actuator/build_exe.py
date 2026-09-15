"""
WHartTest Actuator 打包脚本

使用方法:
    uv run python build_exe.py

依赖:
    pip install pyinstaller

生成文件:
    dist/WHartTest_Actuator/WHartTest_Actuator.exe
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def clean_build():
    """清理之前的构建文件"""
    dirs_to_clean = ['build', 'dist', '__pycache__']
    for dir_name in dirs_to_clean:
        dir_path = Path(dir_name)
        if dir_path.exists():
            print(f"清理目录: {dir_path}")
            shutil.rmtree(dir_path)
    
    # 清理 .pyc 文件
    for pyc_file in Path('.').rglob('*.pyc'):
        pyc_file.unlink()


def bundle_playwright_browser(dist_dir: Path):
    """Install Chromium into the release directory so users do not need a second setup."""
    if os.environ.get('WHARTTEST_BUNDLE_BROWSER', '1').lower() in {'0', 'false', 'no'}:
        print("跳过打包 Chromium（WHARTTEST_BUNDLE_BROWSER=0）")
        return

    configured_browser_dir = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if configured_browser_dir:
        browser_dir = Path(configured_browser_dir)
    elif sys.platform == 'darwin':
        browser_dir = Path.home() / 'Library' / 'Caches' / 'ms-playwright'
    elif sys.platform == 'win32':
        browser_dir = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'ms-playwright'
    else:
        browser_dir = Path.home() / '.cache' / 'ms-playwright'
    browser_dir.mkdir(parents=True, exist_ok=True)
    browser_marker = list(browser_dir.glob('chromium-*'))
    if not browser_marker:
        print(f"未检测到 Chromium，开始下载 {sys.platform} 浏览器运行时...")
        env = os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_dir)
        result = subprocess.run(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            env=env,
            capture_output=False,
        )
        if result.returncode != 0:
            print("Chromium 下载失败!")
            sys.exit(1)

    target_dir = dist_dir / 'browsers'
    shutil.copytree(browser_dir, target_dir, dirs_exist_ok=True)
    print(f"  复制 Chromium: {target_dir}")


def build():
    """执行打包"""
    if sys.platform == 'darwin':
        raise SystemExit('macOS 请使用 build_macos.py 构建 .pkg 安装包')

    print("=" * 50)
    print("WHartTest Actuator 打包工具")
    print("=" * 50)
    
    # 检查 PyInstaller 是否安装
    try:
        import PyInstaller
        print(f"PyInstaller 版本: {PyInstaller.__version__}")
    except ImportError:
        print("错误: 未安装 PyInstaller")
        print("请运行: pip install pyinstaller")
        sys.exit(1)
    
    # 清理之前的构建
    print("\n[1/3] 清理旧的构建文件...")
    clean_build()
    
    # 执行打包
    print("\n[2/3] 开始打包...")
    result = subprocess.run(
        [sys.executable, '-m', 'PyInstaller', 'actuator.spec', '--noconfirm'],
        capture_output=False
    )
    
    if result.returncode != 0:
        print("打包失败!")
        sys.exit(1)
    
    # 复制配置文件
    print("\n[3/3] 复制配置文件...")
    dist_dir = Path('dist/WHartTest_Actuator')
    
    # 复制示例配置文件
    if Path('config.example.toml').exists():
        shutil.copy('config.example.toml', dist_dir / 'config.toml')
        print(f"  复制: config.example.toml -> config.toml")
    
    # 创建数据目录
    (dist_dir / 'data').mkdir(exist_ok=True)
    (dist_dir / 'data' / 'browser').mkdir(exist_ok=True)
    (dist_dir / 'data' / 'screenshots').mkdir(exist_ok=True)
    (dist_dir / 'data' / 'traces').mkdir(exist_ok=True)
    print("  创建: data/ 目录结构")

    bundle_playwright_browser(dist_dir)
    
    # 创建启动脚本
    create_launcher(dist_dir)
    build_installer(dist_dir)
    
    print("\n" + "=" * 50)
    print("打包完成!")
    print(f"输出目录: {dist_dir.absolute()}")
    print("\n使用说明:")
    print("  1. 分发 dist/WHartTest_Actuator_Installer.exe")
    print("  2. 用户双击安装器，程序会自动安装并启动执行器")
    print("  3. 也可以压缩 dist/WHartTest_Actuator/ 目录后使用 install_and_start.bat")
    print("\n首次运行说明:")
    print("  - Chromium 浏览器运行时已随发布包提供")
    print("  - 用户无需额外安装 Python、PyInstaller 或 Playwright")
    print("=" * 50)


def create_launcher(dist_dir: Path):
    """创建启动脚本"""
    # Windows 批处理启动脚本
    bat_content = '''@echo off
chcp 65001 >nul
title WHartTest Actuator

echo ========================================
echo   WHartTest UI 自动化执行器
echo ========================================
echo.

REM 检查是否存在配置文件
if not exist "config.toml" (
    echo [警告] 未找到配置文件，使用默认配置
    echo 请编辑 config.toml 配置服务器地址
    echo.
)

REM 启动执行器（GUI 模式）
echo 启动执行器...
WHartTest_Actuator.exe --gui

echo.
echo 执行器已退出
pause
'''
    
    (dist_dir / 'start.bat').write_text(bat_content, encoding='utf-8')
    print("  创建: start.bat 启动脚本")
    
    # 无 GUI 启动脚本
    bat_nogui_content = '''@echo off
chcp 65001 >nul
title WHartTest Actuator (无GUI模式)

echo ========================================
echo   WHartTest UI 自动化执行器 (无GUI模式)
echo ========================================
echo.

REM 启动执行器（无 GUI 模式，使用配置文件中的账号密码）
WHartTest_Actuator.exe --no-gui

echo.
echo 执行器已退出
pause
'''
    
    (dist_dir / 'start_no_gui.bat').write_text(bat_nogui_content, encoding='utf-8')
    print("  创建: start_no_gui.bat 无GUI启动脚本")

    install_bat_content = r'''@echo off
chcp 65001 >nul
setlocal
title WHartTest Actuator Installer

set "SOURCE_DIR=%~dp0"
set "INSTALL_DIR=%LOCALAPPDATA%\WHartTest\Actuator"

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
robocopy "%SOURCE_DIR%" "%INSTALL_DIR%" /E /XF config.toml /XD data >nul
if errorlevel 8 (
    echo Failed to copy the actuator files.
    pause
    exit /b 1
)

if not exist "%INSTALL_DIR%\config.toml" copy /Y "%SOURCE_DIR%config.toml" "%INSTALL_DIR%\config.toml" >nul
if not exist "%INSTALL_DIR%\data\browser" mkdir "%INSTALL_DIR%\data\browser"
if not exist "%INSTALL_DIR%\data\screenshots" mkdir "%INSTALL_DIR%\data\screenshots"
if not exist "%INSTALL_DIR%\data\traces" mkdir "%INSTALL_DIR%\data\traces"

start "" "%INSTALL_DIR%\WHartTest_Actuator.exe" --gui
echo WHartTest Actuator installed and started.
exit /b 0
'''
    (dist_dir / 'install_and_start.bat').write_text(install_bat_content, encoding='utf-8')
    print("  创建: install_and_start.bat 一键安装并启动脚本")


def build_installer(dist_dir: Path):
    """Build a self-contained EXE that installs and starts the onedir payload."""
    project_dir = Path(__file__).resolve().parent
    payload_dir = dist_dir.resolve()
    installer_dist_dir = project_dir / 'dist'
    installer_work_dir = project_dir / 'build' / 'installer'
    installer_work_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'PyInstaller',
            '--noconfirm',
            '--clean',
            '--onefile',
            '--console',
            '--name',
            'WHartTest_Actuator_Installer',
            '--distpath',
            str(installer_dist_dir),
            '--workpath',
            str(installer_work_dir),
            '--specpath',
            str(installer_work_dir),
            '--add-data',
            f'{payload_dir}{os.pathsep}payload/WHartTest_Actuator',
            '--add-data',
            f'{(project_dir / "config.example.toml").resolve()}{os.pathsep}payload',
            str((project_dir / 'installer.py').resolve()),
        ],
        capture_output=False,
    )
    if result.returncode != 0:
        print("安装器打包失败!")
        sys.exit(1)
    print("  创建: dist/WHartTest_Actuator_Installer.exe")


if __name__ == '__main__':
    os.chdir(Path(__file__).parent)
    build()
