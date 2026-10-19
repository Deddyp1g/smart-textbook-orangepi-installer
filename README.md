# Smart Textbook Orange Pi Installer

这是智慧教材 Orange Pi 特殊算力实例的独立接入安装包。发行包本身包含经过摘要校验的 MindSpore 实验入口、参考脚本和 MNIST 基础数据；安装器会先把这些课程基础文件写入板上的共享目录，再从智慧教材服务器拉取经过白名单和摘要校验的 Agent、Python wheel、匹配的 MindSpore 用户态运行时以及可选的 Ascend 主机栈文件。这样每张新卡在完成审批后都使用同一份实验文件和数据，服务器签名运行包仍负责最终的版本适配。

仓库地址：<https://github.com/Deddyp1g/smart-textbook-orangepi-installer>

## 下载并安装

在全新的 ARM64 Orange Pi 上执行：

```bash
VERSION=0.2.0
curl -fLO "https://github.com/Deddyp1g/smart-textbook-orangepi-installer/releases/download/v${VERSION}/smart-textbook-orangepi-installer-${VERSION}.tar.gz"
curl -fLO "https://github.com/Deddyp1g/smart-textbook-orangepi-installer/releases/download/v${VERSION}/smart-textbook-orangepi-installer-${VERSION}.tar.gz.sha256"
sha256sum -c "smart-textbook-orangepi-installer-${VERSION}.tar.gz.sha256"
tar -xzf "smart-textbook-orangepi-installer-${VERSION}.tar.gz"
cd smart-textbook-orangepi-installer-${VERSION}
sudo ./install.sh
```

安装包没有默认服务器。运行后先输入目标智慧教材服务器的完整地址（含 `http://` 或 `https://` 和所需端口），再隐式输入管理员提供的配对码。安装器只准备配对工具、设备身份并采集只读硬件事实，提交配对申请；等待服务器管理员批准期间，不安装课程数据、CANN 主机栈、Agent 依赖或常驻服务。批准后，安装器才从刚绑定的服务器获取和补齐兼容环境，同步签名运行包、预检并启动 systemd 服务。

无人值守安装可以通过 `--server https://textbook.example.edu --pairing-code <配对码>` 指定地址和配对码；该域名只是示例，应替换为实际部署地址。也可通过 `SMART_TEXTBOOK_AGENT_ENROLLMENT_TOKEN` 环境变量传入配对码，避免在命令行历史中记录。`--enrollment-token` 保持兼容。

配对码由目标服务器管理员提供，不能写入仓库或镜像。`sudo ./install.sh --pair-only` 可以只申请配对，批准后退出；完整部署再用同一服务器和配对码运行安装器。绑定记录仅存放在设备本地的 root 专用状态目录，不含配对码。已绑定设备改连另一台服务器必须显式传入 `--rebind`，并经过新服务器重新批准；等待批准时保留原服务器配置。

## 课程基础包契约

`course-bundle/manifest.json` 固定了本发行包与服务器运行包的对应关系；当前正式 artifact 为 `orangepi-aarch64-ascend310b1-20261019-v23`，需要 `aarch64`、Ascend 310B1、驱动 `25.2.0`、固件 `7.3.7.0.b507`、CANN/ACL `8.0.RC3`、CPython 3.10、MindSpore `2.5.0`、NumPy `1.26.4`、SciPy `1.13.1` 和 safetensors `0.8.0`。`verify-bundle.sh` 可在没有 ARM64 设备的隔离环境中验收包内容（包括清单、独立 checksum、实验入口、参考脚本和 MNIST archive）：

```bash
./verify-bundle.sh --root .
./verify-bundle.sh --root . --json
```

安装器保留课程基础包、`manifest.json` 和独立的 `course-bundle/SHA256SUMS` 供审计；manifest 同时列出四个 MNIST 压缩文件的大小和 SHA-256。`install-course-bundle.sh` 通过临时文件重命名逐项写入持久目录，并在 `/var/lib/smart-textbook/runtime-agent/course-bundle-state.json` 记录 `staging`、`extracting`、`verifying` 或 `complete` 阶段；重复执行会校验并修复中断或损坏的文件。实际 Agent 运行时使用服务器签名 artifact 的 `current` 版本，下载失败或预检失败时不会切换旧版本。

## 后续实验更新

Agent 默认每 300 秒在后台检查兼容的新运行包；已批准设备通过认证心跳续期下载授权，新版本无需重新安装整张卡。服务器签名 manifest 将实验 catalog、入口脚本、基础数据和用户态依赖绑定到同一个不可变 revision。管理员发布新实验的步骤见 [UPDATES.md](UPDATES.md)。

下载、解压和预检在候选目录进行，当前运行包仍可使用。实验和 PTY 在整个使用期间持有共享锁；激活新版本必须取得排他锁，遇到正在使用的学生会保留候选并推迟切换，下一周期重试。文件校验、依赖检查或预检失败都不会切换 `current`；状态会报告待更新版本或失败原因。每次切换采用原子替换，随后 Agent 在学生空闲时加载新版本。

运行包更新维护共享基础文件，不修改 `/home/student` 中学生已有的代码、数据和作业目录。网页课程、实验步骤和界面仍需在智慧教材服务器单独发布，并与 catalog 的实验/步骤标识对应。更新范围受 manifest 兼容性和预检约束，不保证任意驱动、固件或硬件组合都能使用；主机栈变更应按下节流程处理。

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
--no-start             只安装和预检，不启动 systemd
--insecure             仅用于自签名开发服务器
```

## 构建发行包

服务器或发布机上执行：

```bash
./build-package.sh
```

它会读取 `VERSION`，生成对应版本的 `dist/smart-textbook-orangepi-installer-<VERSION>.tar.gz` 和 `.sha256` 文件。构建过程不包含 `wheelhouse/`、Python 缓存、密钥或服务器配置；课程基础包和 MNIST 数据会随发行包包含。
