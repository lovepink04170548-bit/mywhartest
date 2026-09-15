"""One-click Windows installer for the WHartTest actuator package."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


INSTALL_ROOT = Path("WHartTest") / "Actuator"
EXCLUDED_TOP_LEVEL_PATHS = {"data", "config.toml"}


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def payload_dir() -> Path:
    return bundle_root() / "payload" / "WHartTest_Actuator"


def default_install_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / INSTALL_ROOT
    return Path.home() / "AppData" / "Local" / INSTALL_ROOT


def copy_payload(source_dir: Path, install_dir: Path) -> None:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Actuator payload not found: {source_dir}")

    install_dir.mkdir(parents=True, exist_ok=True)
    for source_path in source_dir.rglob("*"):
        relative_path = source_path.relative_to(source_dir)
        if relative_path.parts and relative_path.parts[0] in EXCLUDED_TOP_LEVEL_PATHS:
            continue
        target_path = install_dir / relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)

    config_path = install_dir / "config.toml"
    if not config_path.exists():
        bundled_config = source_dir / "config.toml"
        if bundled_config.exists():
            shutil.copy2(bundled_config, config_path)
        else:
            example_config = bundle_root() / "payload" / "config.example.toml"
            if not example_config.exists():
                raise FileNotFoundError("No actuator configuration template was bundled")
            shutil.copy2(example_config, config_path)

    for directory in ("data/browser", "data/screenshots", "data/traces"):
        (install_dir / directory).mkdir(parents=True, exist_ok=True)


def install_and_start(install_dir: Path) -> int:
    copy_payload(payload_dir(), install_dir)
    executable = install_dir / "WHartTest_Actuator.exe"
    if not executable.exists():
        raise FileNotFoundError(f"Actuator executable not found: {executable}")

    subprocess.Popen([str(executable), "--gui"], cwd=str(install_dir))
    print(f"Installed to: {install_dir}")
    print("The actuator login window is starting.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install and start WHartTest Actuator")
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=default_install_dir(),
        help="Installation directory (default: %%LOCALAPPDATA%%\\WHartTest\\Actuator)",
    )
    return parser.parse_args()


def main() -> int:
    try:
        return install_and_start(parse_args().install_dir.expanduser().resolve())
    except Exception as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        input("Press Enter to exit...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
