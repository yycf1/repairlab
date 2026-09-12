# Docker 容器隔离速记（面试用）

> 一页纸口径。隔离实现在 `src/easycode/runners.py` 的 `DockerRunner.command_run`；约束实测见 `tests/test_runners.py` 的 docker roundtrip 用例与 CI docker 作业。

## 一句话架构

Agent 有四工具（read/bash/edit/write），其中 **bash 是唯一的任意命令入口**——而每条 bash 命令都是一条 `docker run --rm` 的一次性加固容器；模型自己永远碰不到宿主机 shell。

## flag ↔ 威胁对照（docker run 参数逐项）

| 参数 | 挡住的威胁 | 一句话解释 |
| --- | --- | --- |
| `--network none` | 数据外传 / 偷偷联网 | 容器内除 lo 外没有任何网络接口，外连直接失败 |
| `--read-only` | 篡改执行环境 | 根文件系统只读，容器内写不了 /etc 等任何系统位置（bind mount 除外） |
| `--tmpfs /tmp:rw,noexec,nosuid,size=64m` | 落盘再执行 | /tmp 可写但不可执行，且限 64m，写脚本提权这条路堵死 |
| `--cap-drop ALL` | 特权调用 | 丢弃全部 Linux capabilities，无 mount / ptrace 等能力 |
| `--security-opt no-new-privileges` | 提权 | 即使有 suid 二进制也无法获得更多权限 |
| `--user 10001:10001` | 以 root 作恶 | 非 root 运行（镜像里也只建了 10001 用户） |
| `--cpus 1` / `--memory 512m` / `--pids-limit 64` | 资源耗尽 | 死循环烧 CPU、吃内存、fork 炸弹都被封顶 |
| `--pull never` | 隐式联网拉镜像 | 绝不联网拉镜像；镜像必须由操作员预先构建 |
| `--rm` + `finally: docker rm -f` | 残留容器 | 超时/取消后强制清理（finally 兜底，取消杀掉 docker client 也能清干净） |
| `--mount type=bind,…→/workspace`（**可写**） | —— | 工作区就是用户给的真实目录，agent 要在里面改文件——可写是设计而非疏漏 |
| 每条命令唯一 `--name easycode-cmd-<uuid>` | 串扰/清理错对象 | 命名可追踪，超时清理精确命中 |

**`--read-only` 与可写挂载的边界**（Windows/9p 实测）：`--read-only` 只锁容器根文件系统；
bind mount 的写入落在宿主机（uid 10001）。所以容器能改工作区（agent 的本职），但改不了
系统层、装不了依赖、起不了常驻服务。

## 为什么这样设计（三层回答）

1. **pi 默认 YOLO，EasyCode 把"进容器"做成默认项**——pi 作者的建议是"不放心就进容器"，
   我们直接落地为默认后端；可信目录才可切 `LocalRunner`（必须显式 allow_local，
   环境变量里的 KEY/TOKEN/SECRET/PASSWORD 一律过滤，`isolation: trusted_local_only`）。
2. **所有命令走同一 runner 抽象**（`make_runner`）：web 会话与 CLI `easycode run` 拿到的是
   同一个后端对象，没有任何一条路径绕开沙箱。
3. **镜像操作员预构建**（`docker build -f Dockerfile.runner -t easycode-python:latest .`），
   运行时 `--pull never` 不联网装依赖；镜像里只有 python + pytest。

## 诚实边界（面试主动说）

- 不声称防容器逃逸；实测覆盖：非 root UID、根文件系统只读、无外部网络接口、外网连接失败、
  超时终止与容器清理（`tests/test_runners.py::test_docker_run_command_roundtrip_and_timeout`，
  无 Docker 引擎时自动 skip）。
- 工作区可写意味着容器内代码可以改用户文件——这是通用编程 agent 的本职（pi 语义）；
  历史可回放靠会话事件流，文件安全靠用户自己的版本控制。
- LocalRunner 仅限可信目录且需显式开启；本地执行的密钥过滤是环境变量级的，不防进程内读取。

## 亲手验证三个失败（学习实验）

镜像构建后，把 `DockerRunner.command_run` 生成的命令手动跑一遍（工作区换成临时目录）：

```sh
# 1) 系统层只读：向根文件系统写文件 → Read-only file system
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges `
  --user 10001:10001 --cpus 1 --memory 512m --pids-limit 64 `
  easycode-python:latest python -c "open('/etc/x.txt','w')"

# 2) 断网：socket 连 1.1.1.1 → 直接失败
docker run --rm --network none easycode-python:latest python -c "import socket;s=socket.socket();s.settimeout(2);s.connect(('1.1.1.1',443))"

# 3) 非 root：id → uid=10001
docker run --rm --user 10001:10001 easycode-python:latest id
```

## 验证记录位置

- 加固 argv 与 roundtrip/超时用例：`tests/test_runners.py`
- CI：`.github/workflows/ci.yml` docker 作业（Linux：构建镜像 + 跑 roundtrip 用例）
- 沙箱 bash 的工具反馈链：`src/easycode/tools/builtins.py` bash_tool
