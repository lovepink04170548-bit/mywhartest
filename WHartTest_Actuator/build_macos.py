"""Build the macOS WHartTest actuator app and distributable pkg package.

Run this script on macOS because PyInstaller bundles native dependencies for
the host platform:

    python build_macos.py

The primary installer is:

    dist/WHartTest_Actuator_MacOS.pkg

A zip fallback is also generated for environments that do not allow pkg
installation.
"""

from __future__ import annotations

import os
import platform
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from build_exe import bundle_playwright_browser


PROJECT_DIR = Path(__file__).resolve().parent
DIST_DIR = PROJECT_DIR / 'dist'
PYINSTALLER_DIST_DIR = DIST_DIR / '.macos-pyinstaller'
BUILD_DIR = PROJECT_DIR / 'build' / 'macos'
RELEASE_DIR = DIST_DIR / 'WHartTest_Actuator_MacOS'
ARCHIVE_PATH = DIST_DIR / 'WHartTest_Actuator_MacOS.zip'
PKG_PATH = DIST_DIR / 'WHartTest_Actuator_MacOS.pkg'
PKG_ROOT = BUILD_DIR / 'pkg-root'
PKG_SCRIPTS = BUILD_DIR / 'pkg-scripts'
INSTALL_DIR_NAME = 'WHartTest_Actuator'
APP_NAME = 'WHartTest_Actuator.app'


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def clean_build() -> None:
    for path in (PYINSTALLER_DIST_DIR, BUILD_DIR, RELEASE_DIR, ARCHIVE_PATH, PKG_PATH):
        remove_path(path)
    PYINSTALLER_DIST_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)


def run_pyinstaller() -> Path:
    command = [
        sys.executable,
        '-m',
        'PyInstaller',
        'actuator.spec',
        '--noconfirm',
        '--clean',
        '--distpath',
        str(PYINSTALLER_DIST_DIR),
        '--workpath',
        str(BUILD_DIR),
    ]

    target_arch = os.environ.get('MACOS_TARGET_ARCH', '').strip()
    if target_arch:
        command.extend(['--target-architecture', target_arch])

    result = subprocess.run(command, cwd=PROJECT_DIR)
    if result.returncode != 0:
        raise SystemExit('macOS 应用打包失败')

    app_path = PYINSTALLER_DIST_DIR / APP_NAME
    if not app_path.is_dir():
        raise FileNotFoundError(f'未找到 PyInstaller 应用包: {app_path}')
    return app_path


def create_launchers() -> None:
    start_content = f'''#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/{APP_NAME}/Contents/MacOS/WHartTest_Actuator" --gui
'''
    no_gui_content = f'''#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/{APP_NAME}/Contents/MacOS/WHartTest_Actuator" --no-gui
'''

    for filename, content in (
        ('start.command', start_content),
        ('start_no_gui.command', no_gui_content),
    ):
        launcher = RELEASE_DIR / filename
        launcher.write_text(content, encoding='utf-8')
        launcher.chmod(0o755)


def stage_release(app_path: Path) -> None:
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app_path, RELEASE_DIR / APP_NAME)

    shutil.copy2(PROJECT_DIR / 'config.example.toml', RELEASE_DIR / 'config.toml')
    for directory in ('data/browser', 'data/screenshots', 'data/traces', 'data/logs'):
        (RELEASE_DIR / directory).mkdir(parents=True, exist_ok=True)

    bundle_playwright_browser(RELEASE_DIR)
    create_launchers()


def zip_release() -> None:
    with zipfile.ZipFile(ARCHIVE_PATH, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source_path in RELEASE_DIR.rglob('*'):
            if not source_path.is_file():
                continue
            relative_path = source_path.relative_to(RELEASE_DIR)
            archive_name = Path(RELEASE_DIR.name) / relative_path
            archive.write(source_path, arcname=str(archive_name))


def create_pkg_postinstall() -> None:
    postinstall = PKG_SCRIPTS / 'postinstall'
    postinstall.parent.mkdir(parents=True, exist_ok=True)
    postinstall.write_text(
        '''#!/bin/bash
set -u

APP_PATH="/Applications/WHartTest_Actuator/WHartTest_Actuator.app"
CONSOLE_USER="$(/usr/bin/stat -f '%Su' /dev/console 2>/dev/null || true)"

if [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ] || [ ! -d "$APP_PATH" ]; then
    exit 0
fi

CONSOLE_UID="$(/usr/bin/id -u "$CONSOLE_USER" 2>/dev/null || true)"
if [ -n "$CONSOLE_UID" ]; then
    /bin/launchctl asuser "$CONSOLE_UID" /usr/bin/sudo -u "$CONSOLE_USER" \
        /usr/bin/open "$APP_PATH" >/dev/null 2>&1 || true
fi

exit 0
''',
        encoding='utf-8',
    )
    postinstall.chmod(0o755)


def sign_app(app_path: Path) -> None:
    identity = os.environ.get('MACOS_APP_SIGNING_IDENTITY', '').strip()
    if not identity:
        return

    result = subprocess.run([
        'codesign',
        '--deep',
        '--force',
        '--options',
        'runtime',
        '--timestamp',
        '--sign',
        identity,
        str(app_path),
    ], cwd=PROJECT_DIR)
    if result.returncode != 0:
        raise SystemExit('macOS .app 代码签名失败')


def build_pkg(app_path: Path) -> None:
    if shutil.which('pkgbuild') is None:
        raise SystemExit('未找到 pkgbuild，请在 macOS 主机上运行此脚本')

    pkg_install_dir = PKG_ROOT / 'Applications' / INSTALL_DIR_NAME
    pkg_install_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(app_path, pkg_install_dir / APP_NAME)
    shutil.copy2(PROJECT_DIR / 'config.example.toml', pkg_install_dir / 'config.toml')
    bundle_playwright_browser(pkg_install_dir)
    sign_app(pkg_install_dir / APP_NAME)
    create_pkg_postinstall()

    version = os.environ.get('MACOS_PACKAGE_VERSION', '1.0.0').strip() or '1.0.0'
    command = [
        'pkgbuild',
        '--root',
        str(PKG_ROOT),
        '--scripts',
        str(PKG_SCRIPTS),
        '--identifier',
        'com.wharttest.actuator',
        '--version',
        version,
        '--install-location',
        '/',
    ]
    installer_identity = os.environ.get('MACOS_INSTALLER_SIGNING_IDENTITY', '').strip()
    if installer_identity:
        command.extend(['--sign', installer_identity])
    command.append(str(PKG_PATH))
    result = subprocess.run(command, cwd=PROJECT_DIR)
    if result.returncode != 0:
        raise SystemExit('macOS .pkg 安装包构建失败')


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    if sys.platform != 'darwin':
        raise SystemExit('macOS 安装包必须在 macOS 主机上构建')

    print(f'构建 macOS 执行器，架构: {platform.machine()}')
    clean_build()
    app_path = run_pyinstaller()
    stage_release(app_path)
    zip_release()
    build_pkg(app_path)

    print('=' * 60)
    print(f'发布目录: {RELEASE_DIR}')
    print(f'压缩包: {ARCHIVE_PATH}')
    print(f'安装包: {PKG_PATH}')
    print(f'PKG SHA256: {file_sha256(PKG_PATH)}')
    print(f'ZIP SHA256: {file_sha256(ARCHIVE_PATH)}')
    print('用户下载 .pkg 后双击安装，安装完成后会尝试自动启动执行器。')
    print('=' * 60)


if __name__ == '__main__':
    main()
