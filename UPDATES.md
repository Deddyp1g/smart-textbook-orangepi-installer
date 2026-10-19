# 管理员发布后续实验

安装器 0.2.0 对应运行包 `orangepi-aarch64-ascend310b1-20261019-v23`。设备先由用户输入的服务器地址和配对码完成配对，获批后才部署。后续实验通过已绑定服务器发布新签名运行包，不需要把安装器重新写入每张卡。每次发布都使用新的唯一 revision；已经注册的 revision 不可修改、覆盖或复用。

## 准备候选目录

在受控构建目录准备完整运行包，包含经过测试的 Agent/同步器、`preflight.py`、固定 PTY 启动器、实验入口、参考脚本、基础数据归档、用户态依赖归档及 `runtime-install.json`。可从已知良好的构建源复制共享内容，保留旧版作为回退；不要从学生作业目录打包。

新增实验时，在 `experiments/catalog.json` 中注册实验标识、步骤标识、相对入口路径和超时，例如：

```json
{
  "reference_mindspore": {
    "training": {"path": "experiments/mindspore-training", "timeout_seconds": 900},
    "evaluate": {"path": "experiments/mindspore-evaluate", "timeout_seconds": 300},
    "infer": {"path": "experiments/mindspore-infer", "timeout_seconds": 300}
  },
  "reference_new": {
    "run": {"path": "experiments/new-run", "timeout_seconds": 300}
  }
}
```

catalog 路径必须指向包内真实文件，超时范围为 5–900 秒。导入器将 catalog 写入签名 manifest；入口、参考脚本、数据和依赖归档同时记录大小及 SHA-256。当前导入器仍要求包含 MindSpore 参考实验和 MNIST 基础契约，新实验可与之共存。归档须在 `runtime-install.json` 声明 kind、目标相对目录及正整数 `unpacked_size`，不同归档的解压目标不能重叠；可按受支持的策略在验证解压后消费压缩包。

兼容性改变时准备完整的 `compatibility.json`，确认架构、设备型号、驱动/固件、CANN/ACL、Python ABI 和 MindSpore 约束与真实设备相符。先在隔离设备验证包和新实验的完整执行流程。用户态包更新不保证任意驱动或固件兼容；需要安装或重启的主机栈变更单独走授权主机栈流程。

## 使用线上环境导入并签名

下例在当前服务器上运行，修改 `source` 与新唯一的 `revision`。它从线上环境 JSON 读取数据库、资产目录和签名配置，合入子进程环境后调用 canonical 导入器。不要使用开发仓库旧 `.env` 代替线上环境，也不要打印、复制到文档或提交环境 JSON、私钥和下载令牌。

```bash
cd /root/smart-textbook
/root/smart-textbook/.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path
import subprocess

source = Path("/absolute/path/to/verified-new-runtime")
revision = "orangepi-aarch64-ascend310b1-YYYYMMDD-vNN"
environment_path = Path("/root/smart-textbook-maintenance-20260916/acceptance-environment.json")
environment = os.environ.copy()
environment.update({str(key): str(value) for key, value in json.loads(environment_path.read_text()).items()})
command = [
    "/root/smart-textbook/.venv/bin/python",
    "/root/smart-textbook/maintenance/runtime_artifacts/import_verified_orangepi.py",
    "--source", str(source),
    "--revision", revision,
]
# 需要改变兼容性时，提供已经验证的完整约束文件：
# command += ["--compatibility-json", "/absolute/path/to/compatibility.json"]
subprocess.run(command, env=environment, cwd="/root/smart-textbook", check=True)
PY
```

命令会校验源目录、生成所有文件摘要、验证实验 catalog、签名并注册 active 运行包；完成后记录输出的 revision、manifest 摘要、文件数和资产目录。只有经审核后才运行实际导入；导入即进入兼容设备的新版本选择范围。`--no-register` 仍会在配置的资产目录写入候选并签名，不是无副作用的检查模式。

## 验收设备状态

设备每 300 秒检查更新，认证心跳也会通知新的兼容版本并续期下载授权。下载、解压、校验和预检不会阻断当前运行包；新版本尚未完成时，已有学生继续使用旧版。

学生实验和 PTY 持有共享运行锁，候选激活只尝试非阻塞排他锁。忙时保留已验证候选，旧版继续 ready，并报告 `update_pending=true` 与 `desired_artifact_revision`；学生释放后再由后台周期重试。校验或预检失败时不切换 current，应先按错误修正构建源，再发布另一个新 revision。确认目标设备报告的新 revision、基础包和 catalog 状态 ready，并执行新旧实验入口验收；保留已知良好的运行包供管理员回退决策。

共享运行包更新不修改 `/home/student` 的已有学生目录和作业。网页课程、步骤说明、作业配置和界面属于服务器应用发布，需要单独部署，并确保调用的实验/步骤标识与新 catalog 一致。
