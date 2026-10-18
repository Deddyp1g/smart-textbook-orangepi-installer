# Smart Textbook Orange Pi Installer

这是智慧教材 Orange Pi 特殊算力实例的独立接入安装包。发行包本身包含经过摘要校验的 MindSpore 实验入口、参考脚本和 MNIST 基础数据；安装器会先把这些课程基础文件写入板上的共享目录，再从智慧教材服务器拉取经过白名单和摘要校验的 Agent、Python wheel、匹配的 MindSpore 用户态运行时以及可选的 Ascend 主机栈文件。这样每张新卡在完成审批后都使用同一份实验文件和数据，服务器签名运行包仍负责最终的版本适配。

仓库地址：<https://github.com/Deddyp1g/smart-textbook-orangepi-installer>

## 下载并安装

在全新的 ARM64 Orange Pi 上执行：

```bash
VERSION=0.1.4
curl -fLO "https://github.com/Deddyp1g/smart-textbook-orangepi-installer/releases/download/v${VERSION}/smart-textbook-orangepi-installer-${VERSION}.tar.gz"
curl -fLO "https://github.com/Deddyp1g/smart-textbook-orangepi-installer/releases/download/v${VERSION}/smart-textbook-orangepi-installer-${VERSION}.tar.gz.sha256"
sha256sum -c "smart-textbook-orangepi-installer-${VERSION}.tar.gz.sha256"
tar -xzf "smart-textbook-orangepi-installer-${VERSION}.tar.gz"
cd smart-textbook-orangepi-installer-${VERSION}
sudo ./install.sh \
  --server http://server.example.invalid:5173 \
  --enrollment-token '<一次性接入令牌>' \
  --host-stack auto
```

安装器会先运行 `verify-bundle.sh`，校验课程清单中 5 个基础文件（3 个实验入口、参考脚本、MNIST 压缩包）的 SHA-256、大小和运行时契约，并把数据解压到 `/usr/local/share/smart-textbook/data/mnist`。随后它检查 ARM64、Python ABI、Ascend/NPU 事实和 CANN 版本，生成设备 Ed25519 身份，下载并校验服务器端依赖，然后向服务器申请接入。服务器管理员批准设备并准备兼容的运行时后，设备会自动同步签名运行包、再次预检并以 systemd 常驻服务运行。

一次性接入令牌由服务器管理员提供，不能写入仓库或镜像。设备审批完成前不会启动学生可用的运行时。

## 课程基础包契约

`course-bundle/manifest.json` 固定了本发行包与服务器运行包的对应关系：当前 `20261018-v14` 需要 `aarch64`、Ascend 310B1、驱动 `25.2.0`、固件 `7.3.7.0.b507`、CANN/ACL `8.0.RC3`、CPython 3.10、MindSpore `2.5.0`、NumPy `1.26.4`、SciPy `1.13.1` 和 safetensors `0.8.0`。`verify-bundle.sh` 可在没有 ARM64 设备的隔离环境中验收包内容：

```bash
./verify-bundle.sh --root .
./verify-bundle.sh --root . --json
```

安装器保留课程基础包和 manifest 供审计；实际 Agent 运行时使用服务器签名 artifact 的 `current` 版本，下载失败或预检失败时不会切换旧版本。

## 主机栈和恢复

`--host-stack auto` 是默认模式。服务器有兼容的 manifest 时，安装器只接受声明的 `.deb`、Ascend `.run` 和 `.tar.gz` 文件，并逐个校验大小和 SHA-256。需要重启的驱动、固件或 CANN 安装会写入持久化恢复状态，重启后由 `smart-textbook-host-stack-resume.service` 继续执行。

如果服务器暂时没有官方主机栈包，安装器会记录 `unavailable / existing_host_environment` 并保留现有环境，不会伪称驱动或 CANN 已安装。仓库不包含 Ascend 专有二进制；管理员应按厂商许可把授权文件放入服务器的 host-stack 资产目录并生成 manifest。

## 常用选项

```text
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
