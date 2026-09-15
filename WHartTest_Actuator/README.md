# UI自动化执行器 (WHartTest Actuator)

独立的UI自动化执行器服务，通过WebSocket连接Django后端，接收并执行自动化测试任务。

## 架构设计


```
┌─────────────┐     WebSocket      ┌──────────────────┐
│   Django    │ ◄────────────────► │   Actuator       │
│   Backend   │                    │   (执行器)        │
│             │                    │                  │
│ - Consumer  │ ◄─ 发送任务 ────── │ - WebSocketClient│
│ - 任务分发   │ ◄─ 返回结果 ────── │ - TaskConsumer   │
└─────────────┘                    │ - Executor       │
                                   └──────────────────┘
                                          │
                                          ▼
                                   ┌──────────────────┐
                                   │  Playwright      │
                                   │  (浏览器自动化)   │
                                   └──────────────────┘
```

## 通信协议

### 消息格式 (SocketDataModel)
```json
{
    "code": 200,
    "msg": "success",
    "user": "username",
    "is_notice": 2,
    "data": {
        "func_name": "u_test_case",
        "func_args": {
            "case_id": 1
        }
    }
}
```

### 支持的任务类型
- `u_page_steps` - 执行页面步骤
- `u_test_case` - 执行测试用例
- `u_test_case_batch` - 批量执行用例
- `u_stop_execution` - 停止执行
- `u_step_result` - 步骤执行结果
- `u_case_result` - 用例执行结果

## 安装

### Windows 一键安装（推荐）

发布版本推荐分发：

```text
dist/WHartTest_Actuator_Installer.exe
```

用户双击该 `.exe` 后，安装器会自动：

1. 将执行器安装到 `%LOCALAPPDATA%\WHartTest\Actuator`
2. 保留用户已有的 `config.toml`，不会覆盖已有配置
3. 创建浏览器用户数据、截图和 Trace 目录
4. 自动启动执行器 GUI 登录窗口

Chromium 浏览器运行时已包含在发布包中，用户无需另外安装 Python、PyInstaller、Playwright 或浏览器。

如果无法使用单文件安装器，也可以分发完整的 `dist/WHartTest_Actuator/` 目录，用户双击其中的 `install_and_start.bat` 完成安装并启动。

### macOS 一键安装包

macOS `.pkg` 安装包必须在 macOS 主机上构建，不能在 Windows/Linux 上交叉生成。构建命令：

```bash
cd WHartTest_Actuator
python build_macos.py
```

脚本默认生成当前 Mac 架构的原生包；如需显式指定架构，可使用
`MACOS_TARGET_ARCH=arm64` 或 `MACOS_TARGET_ARCH=x86_64`。

构建前请安装当前 Mac 架构对应的 Python 依赖：

```bash
python -m pip install -r requirements.txt
python -m pip install pyinstaller pyside6 playwright
python -m playwright install chromium
```

构建结果：

```text
dist/WHartTest_Actuator_MacOS.pkg
```

构建脚本也会同时生成一个 zip 备用包，并分别输出 `.pkg` 和 `.zip` 的 `SHA256`。
接入指南默认提供 `.pkg`。如需在接入指南中显示校验值，
将它配置到后端环境变量：

```dotenv
UI_ACTUATOR_PACKAGE_MACOS_FILE_NAME=WHartTest_Actuator_MacOS.pkg
UI_ACTUATOR_PACKAGE_MACOS_SHA256=<构建脚本输出的 PKG SHA256>
```

仓库中的 `.github/workflows/build-macos-package.yml` 会在 `develop` 或 `master` 分支变更执行器代码时，
使用 GitHub Actions 的 `macos-latest` 自动构建 macOS 安装包，并上传到该次运行的
Actions Artifact。推送形如 `v1.0.0` 的 Git 标签时，工作流还会把 `.pkg` 和 SHA256
文件发布到 GitHub Release。Release 资产地址可直接配置到后端：

```dotenv
UI_ACTUATOR_PACKAGE_MACOS_URL=https://github.com/MGdaasLab/WHartTest/releases/download/v1.0.0/WHartTest_Actuator_MacOS.pkg
UI_ACTUATOR_PACKAGE_MACOS_FILE_NAME=WHartTest_Actuator_MacOS.pkg
```

本地 Docker Compose 已将 `WHartTest_Actuator/dist` 挂载到后端的
`/app/actuator-packages`，把 `.pkg` 复制到该目录后重启后端即可被接入指南发现。
如果使用外部文件服务器，也可以设置 `UI_ACTUATOR_PACKAGE_MACOS_URL`，此时接入指南会直接使用该地址。

用户下载 `WHartTest_Actuator_MacOS.pkg` 后双击，按 macOS 安装向导完成安装。
安装目录为 `/Applications/WHartTest_Actuator/`，安装完成后脚本会尝试自动启动执行器。
配置、日志、截图和浏览器用户数据写入当前用户的
`~/Library/Application Support/WHartTest/Actuator/`，不会写入 `/Applications`。
未签名/notarized 的包首次可能被 macOS 拦截，需要右键选择“打开”，或在“系统设置 -> 隐私与安全性”中允许。

如果 macOS 主机已安装 Apple Developer ID 证书，可以在构建前配置签名身份：

```bash
export MACOS_APP_SIGNING_IDENTITY="Developer ID Application: Your Company (TEAMID)"
export MACOS_INSTALLER_SIGNING_IDENTITY="Developer ID Installer: Your Company (TEAMID)"
python build_macos.py
```

代码签名可以减少 Gatekeeper 拦截；正式对外分发还需要使用 Apple notarization 流程。

macOS 备用 zip 发布目录包含：

```text
WHartTest_Actuator_MacOS/
├── WHartTest_Actuator.app
├── config.toml
├── start.command
├── start_no_gui.command
├── browsers/
└── data/
```

### 源码安装

```bash
cd WHartTest_Actuator
pip install -r requirements.txt
```

## 使用

### 基本启动
```bash
python main.py
```

### 指定服务器地址
```bash
python main.py --server ws://192.168.1.100:8000/ws/ui/actuator/
```

### 指定执行器ID
```bash
python main.py --id actuator-01 --server ws://localhost:8000/ws/ui/actuator/
```

### 完整参数
```bash
python main.py \
    --server ws://localhost:8000/ws/ui/actuator/ \
    --api http://localhost:8000 \
    --id my-actuator \
    --log-level DEBUG
```

## 参数说明

| 参数 | 短参数 | 默认值 | 说明 |
|------|--------|--------|------|
| --server | -s | ws://localhost:8000/ws/ui/actuator/ | WebSocket服务器地址 |
| --api | -a | http://localhost:8000 | API服务器地址 |
| --id | -i | actuator-{pid} | 执行器唯一标识 |
| --log-level | -l | INFO | 日志级别 |

## 工作流程

1. **连接服务器**: 执行器启动后通过WebSocket连接Django后端
2. **等待任务**: 监听来自服务器的执行任务
3. **获取详情**: 通过REST API获取用例/步骤详细信息
4. **生成脚本**: 将步骤配置转换为Playwright代码
5. **执行测试**: 调用Playwright执行浏览器自动化
6. **返回结果**: 通过WebSocket将执行结果发送回服务器

## 打包成独立 EXE

执行器支持打包成独立可执行文件，方便分发部署。

### 安装打包依赖

```bash
# 使用 uv
uv pip install pyinstaller

# 或使用 pip
pip install pyinstaller
```

### 执行打包

```bash
cd WHartTest_Actuator
uv run python build_exe.py
```

### 输出目录

```
dist/WHartTest_Actuator/
├── WHartTest_Actuator.exe  # 主程序
├── config.toml             # 配置文件
├── start.bat               # GUI模式启动脚本
├── start_no_gui.bat        # 无GUI模式启动脚本
├── install_and_start.bat   # 目录版一键安装并启动
├── browsers/               # 随包提供的 Playwright Chromium
└── data/                   # 数据目录
    ├── browser/            # 浏览器用户数据
    ├── screenshots/        # 截图
    └── traces/             # Trace文件
```

### 使用说明

推荐直接运行 `dist/WHartTest_Actuator_Installer.exe`。安装后编辑：

```text
%LOCALAPPDATA%\WHartTest\Actuator\config.toml
```

然后重新启动执行器。目录版也可以直接编辑发布目录中的 `config.toml` 后运行 `start.bat`。

## 分布式部署

执行器支持分布式部署，多个执行器可以同时连接到一个Django后端：

```bash
# 机器A
python main.py --id actuator-machine-a --server ws://server:8000/ws/ui/actuator/

# 机器B  
python main.py --id actuator-machine-b --server ws://server:8000/ws/ui/actuator/

# 机器C
python main.py --id actuator-machine-c --server ws://server:8000/ws/ui/actuator/
```

服务器会自动将任务分发给可用的执行器。

## 文件说明

```
WHartTest_Actuator/
├── main.py              # 主入口，启动执行器
├── models.py            # 消息模型定义
├── websocket_client.py  # WebSocket客户端
├── consumer.py          # 任务消费者
├── executor.py          # Playwright执行引擎
├── browser_installer.py # 浏览器安装检查模块
├── build_exe.py         # 打包脚本
├── build_macos.py       # macOS .pkg、.app 和 zip 打包脚本
├── actuator.spec        # PyInstaller配置
├── requirements.txt     # 依赖
└── README.md            # 说明文档
```
