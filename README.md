# Smart Textbook Orange Pi Installer

这是智慧教材 Orange Pi 特殊算力实例的独立接入安装包。新板只需要下载本仓库的发行包，安装器就会从智慧教材服务器拉取经过白名单和摘要校验的 Agent、Python wheel、实验入口、MindSpore 用户态运行时以及可选的 Ascend 主机栈文件。

仓库地址：<https://github.com/Deddyp1g/smart-textbook-orangepi-installer>

## 下载并安装

在全新的 ARM64 Orange Pi 上执行：

```bash
VERSION=0.1.1
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

安装器会检查 ARM64、Python ABI、Ascend/NPU 事实和 CANN 版本，生成设备 Ed25519 身份，下载并校验服务器端依赖，然后向服务器申请接入。服务器管理员批准设备并准备兼容的运行时后，设备会自动同步运行时并以 systemd 常驻服务运行。

一次性接入令牌由服务器管理员提供，不能写入仓库或镜像。设备审批完成前不会启动学生可用的运行时。

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

它会生成 `dist/smart-textbook-orangepi-installer-0.1.0.tar.gz` 和对应的 `.sha256` 文件。构建过程不包含 `wheelhouse/`、Python 缓存、密钥或服务器配置。
