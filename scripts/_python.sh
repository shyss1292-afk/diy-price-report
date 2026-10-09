#!/usr/bin/env bash
# 统一的 Python 解释器解析 —— 各脚本 `source` 本文件后即可用 $PYTHON_BIN。
#
# 为什么必须抽出来
# ---------------
# 原先 5 个脚本各自硬编码了**作者本机的 venv 绝对路径**（形如
# `/Users/<用户名>/.workbuddy/.../envs/<env>/bin/python`）。别人 clone 下来
# 这些脚本直接跑不了，而且失败信息只有一句 `No such file or directory`，
# 看不出是路径问题。
#
# 解析顺序（从具体到通用）：
#   1. $DIYPRICE_PYTHON     —— 显式指定，最高优先级（部署/CI 里常用）
#   2. 项目内 .venv / venv  —— Python 项目的标准做法
#   3. 系统 python3         —— 兜底
#
# 用法：`source "$(dirname "${BASH_SOURCE[0]}")/_python.sh"`
#       本文件**自己推导**项目根目录，不依赖调用方先定义 PROJECT_DIR。

# ⚠️ `${BASH_SOURCE[0]:-$0}` 而不是只写 `${BASH_SOURCE[0]}`：
#    BASH_SOURCE 是 **bash 专有**变量，在 zsh 里为空 —— 一旦有人
#    `source scripts/_python.sh`（zsh 是 macOS 默认 shell），
#    `dirname ""` 会退化成 `.`，路径推导就跑到**父目录**去了，
#    结果静默取到系统 python（没有 playwright）而不是项目 venv。
#    加 `:-$0` 让 zsh 下也能正确解析（zsh 用 $0 表示被 source 的文件）。
_PY_HELPER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

if [ -n "${DIYPRICE_PYTHON:-}" ] && [ -x "${DIYPRICE_PYTHON}" ]; then
  PYTHON_BIN="$DIYPRICE_PYTHON"
elif [ -x "$_PY_HELPER_ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$_PY_HELPER_ROOT/.venv/bin/python"
elif [ -x "$_PY_HELPER_ROOT/venv/bin/python" ]; then
  PYTHON_BIN="$_PY_HELPER_ROOT/venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || echo python3)"
fi
export PYTHON_BIN
