# Smart Textbook Orange Pi Installer

这是智慧教材 Orange Pi 算力节点的接入安装包。安装包包含设备 Agent、ARM64 容器构建文件、MindSpore 实验入口、参考脚本和经摘要验证的 MNIST 基础数据。管理员批准配对后，安装器从配对服务器同步签名课程和运行时，按当前设备的 Ascend 驱动、固件、CANN 和 Python ABI 预检。兼容平台是 ARM64 Ubuntu 22.04。容器使用本机 Docker Unix socket。

仓库地址：<https://github.com/Deddyp1g/smart-textbook-orangepi-installer>

## 下载并安装

在全新的 ARM64 Orange Pi 上执行：

```bash
VERSION=0.2.2
curl -fLO "https://github.com/Deddyp1g/smart-textbook-orangepi-installer/releases/download/v${VERSION}/smart-textbook-orangepi-installer-${VERSION}.tar.gz"
curl -fLO "https://github.com/Deddyp1g/smart-textbook-orangepi-installer/releases/download/v${VERSION}/smart-textbook-orangepi-installer-${VERSION}.tar.gz.sha256"
sha256sum -c "smart-textbook-orangepi-installer-${VERSION}.tar.gz.sha256"
tar -xzf "smart-textbook-orangepi-installer-${VERSION}.tar.gz"
cd smart-textbook-orangepi-installer-${VERSION}
sudo ./install.sh
```

运行安装器后，输入目标智慧教材服务器地址（含 `http://` 或 `https://` 和所需端口），再输入管理员提供的配对码。等候批准时，安装器准备配对工具和设备身份，并采集只读硬件信息。批准后，安装器准备 Docker 工作区和课程数据，同步并预检服务器签名的运行包，再启动 systemd 服务并等待设备通过认证。

升级已经在线的节点前，在管理端释放它的实验租约并暂停调度。重复执行安装器会沿用板上的身份。切换时，安装器复核容器已释放，遇到认证失败会恢复原 Agent 文件和服务配置。

升级时解压新安装包，进入目录执行 `sudo ./install.sh --host-stack verify --verify-npu`。输入原服务器地址和当前有效的配对码。已有稳定课程镜像的设备可用 `--workspace-image sha256:ID` 复用该镜像。完成后在管理端恢复调度。

无人值守安装可以通过 `--server https://textbook.example.edu --pairing-code <配对码>` 指定地址和配对码；该域名只是示例，应替换为实际部署地址。也可通过 `SMART_TEXTBOOK_AGENT_ENROLLMENT_TOKEN` 环境变量传入配对码，避免在命令行历史中记录。`--enrollment-token` 保持兼容。

配对码由目标服务器管理员提供，不能写入仓库或镜像。`sudo ./install.sh --pair-only` 可以只申请配对，批准后退出；完整部署再用同一服务器和配对码运行安装器。绑定记录仅存放在设备本地的 root 专用状态目录，不含配对码。已绑定设备改连另一台服务器必须显式传入 `--rebind`，并经过新服务器重新批准；等待批准时保留原服务器配置。

## 课程基础包契约

`course-bundle/manifest.json` 记录安装包内置的 MindSpore 实验和 MNIST 数据。目标服务器可以提供更高版本的签名运行包，安装器核对它与配对批准的版本一致，并验证本机兼容性。内置课程环境使用 `aarch64`、Ascend 310B1、驱动 `25.2.0`、固件 `7.3.7.0.b507`、CANN/ACL `8.0.RC3`、CPython 3.10、MindSpore `2.5.0`、NumPy `1.26.4`、SciPy `1.13.1` 和 safetensors `0.8.0`。可以在任意架构验收内置课程及数据。

```bash
./verify-bundle.sh --root .
./verify-bundle.sh --root . --json
```

安装器保留课程基础包、`manifest.json` 和独立的 `course-bundle/SHA256SUMS` 供审计；manifest 同时列出四个 MNIST 压缩文件的大小和 SHA-256。`install-course-bundle.sh` 通过临时文件重命名逐项写入持久目录，并在 `/var/lib/smart-textbook/runtime-agent/course-bundle-state.json` 记录 `staging`、`extracting`、`verifying` 或 `complete` 阶段；重复执行会校验并修复中断或损坏的文件。实际 Agent 运行时使用服务器签名 artifact 的 `current` 版本，下载失败或预检失败时不会切换旧版本。

## 后续实验更新

Agent 默认每 300 秒在后台检查兼容的新运行包。已批准设备通过认证心跳续期下载授权。服务器签名 manifest 将实验 catalog、入口、基础数据和用户态依赖绑定到同一个 revision。设备 Agent 和容器适配组件通过本安装包一起升级。管理员发布新实验的步骤见 [UPDATES.md](UPDATES.md)。

下载、解压和预检在候选目录进行，当前运行包仍可使用。实验和 PTY 在整个使用期间持有共享锁；激活新版本必须取得排他锁，遇到正在使用的学生会保留候选并推迟切换，下一周期重试。文件校验、依赖检查或预检失败都不会切换 `current`；状态会报告待更新版本或失败原因。每次切换采用原子替换，随后 Agent 在学生空闲时加载新版本。

网页课程、实验步骤和界面在智慧教材服务器发布，并与 catalog 的实验和步骤标识对应。主机栈变更按下节流程处理。

## CPU 与 Ascend 工作区

工作区以物理设备为调度单位。同一设备每次接受一个租约，归还后销毁对应容器。学生以 UID 20000 使用自己的临时 `/home/student`。CPU 容器通过隔离和写入检查后可用，Ascend 容器需要在本机通过 ACL 和 MindSpore 张量运算。换镜像或切换 Ascend 运行包后，根据管理端状态重新验证。

安装器默认验证 Canonical Ubuntu Base 22.04.5 ARM64 的 SHA-256，并在设备本地构建课程镜像。可用 `--workspace-base-archive FILE` 指定已下载的同一归档，或用 `--workspace-image sha256:ID` 指定管理员已核验的本地镜像。安装器检查镜像的架构和用户，并实际启动一次 CPU 容器。容器运行期间禁用网络，课程镜像构建需要访问 Ubuntu ARM64 软件源。

在设备空闲时使用 `sudo ./install.sh --verify-npu` 配对、安装并验收，或在已安装的设备上单独运行验收。

```bash
sudo /opt/smart-textbook/runtime-agent/venv/bin/python -B \
  /opt/smart-textbook/runtime-agent/scoped_container.py --verify-npu
```

NPU 验收成功后，板卡会在下一次心跳更新时上报可用。验收失败会给出设备或运行时错误，可在修复后重新执行。镜像、驱动和授权的 Ascend 用户态组件需要在每台板上匹配。

## 主机栈和恢复

`--host-stack auto` 是默认模式。服务器有兼容的 manifest 时，安装器只接受声明的 `.deb`、Ascend `.run` 和 `.tar.gz` 文件，并逐个校验大小和 SHA-256。需要重启的驱动、固件或 CANN 安装会写入持久化恢复状态，重启后由 `smart-textbook-host-stack-resume.service` 继续执行。

如果服务器暂时没有官方主机栈包，安装器会记录 `unavailable / existing_host_environment` 并保留现有环境，不会伪称驱动或 CANN 已安装。仓库不包含 Ascend 专有二进制；管理员应按厂商许可把授权文件放入服务器的 host-stack 资产目录并生成 manifest。

## 常用选项

```text
--server URL           配对目标服务器，无预设地址
--pairing-code CODE    管理员提供的配对码（兼容 --enrollment-token）
--pair-only            配对获批后退出，不部署运行环境
--rebind               显式申请切换到另一台服务器
--host-stack auto|verify|never
--device-id ID
--display-name NAME
--timeout SECONDS
--wheelhouse DIR       离线 wheelhouse（默认从服务器拉取）
--no-start             安装并预检，Docker 已运行时跳过启动 Agent
--workspace-image sha256:ID
--workspace-base-archive FILE
--verify-npu           安装后执行板上 NPU 容器验收
--insecure             仅用于自签名开发服务器
```

## 构建发行包

服务器或发布机上执行：

```bash
./build-package.sh
```

它会读取 `VERSION`，生成对应版本的 `dist/smart-textbook-orangepi-installer-<VERSION>.tar.gz` 和 `.sha256` 文件。构建过程不包含 `wheelhouse/`、Python 缓存、密钥或服务器配置；课程基础包和 MNIST 数据会随发行包包含。
