#!/bin/sh
# One-command Linux entry point for the vpsops Gateway or VPS Agent.
set -eu

REPOSITORY=https://github.com/Saito-Sakuya/vpsops.git
ARCHIVE=https://github.com/Saito-Sakuya/vpsops/archive/refs/heads/master.tar.gz
REPO_DIR=/opt/vpsops
CONFIG_DIR=/etc/vpsops-gateway
TMP_DIR=

usage() {
    cat <<'EOF'
用法：
  install.sh gateway
  install.sh agent "ssh-ed25519 AAAA... vpsops-gateway" 100.64.0.5

示例：
  curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- gateway
  curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- agent "ssh-ed25519 AAAA... vpsops-gateway" 100.64.0.5
EOF
}

fail() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [ -n "$TMP_DIR" ]; then
        rm -rf -- "$TMP_DIR"
    fi
}

make_tmp_dir() {
    if [ -z "$TMP_DIR" ]; then
        TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/vpsops-install.XXXXXX") || fail '无法创建临时目录'
        trap cleanup 0
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
    fi
}

require_root() {
    [ "$(id -u)" = 0 ] || fail '请使用 root 运行（例如 sudo sh -s -- gateway）'
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

gateway_prerequisites() {
    require_command apt-get
    require_command systemctl
    [ -d /run/systemd/system ] || fail '需要正在运行的 systemd'
    require_command tailscale
    systemctl is-active --quiet tailscaled || fail '需要已启动并登录的 Tailscale（tailscaled）'
    tailscale status >/dev/null 2>&1 || fail 'Tailscale 尚未就绪；请先连接到 tailnet'
    [ -n "$(tailscale ip -4 2>/dev/null || true)" ] || fail 'Tailscale 尚未分配 IPv4 地址'

    [ -r /etc/os-release ] || fail '无法识别 Linux 发行版；仅支持 Debian 12 和 Ubuntu 24.04'
    . /etc/os-release
    case "${ID-}:${VERSION_ID-}" in
        debian:12|ubuntu:24.04) ;;
        *) fail "仅支持 Debian 12 和 Ubuntu 24.04（当前：${PRETTY_NAME-未知}）" ;;
    esac
}

prepare_tmp_source() {
    make_tmp_dir
    curl -fLsS --output "$TMP_DIR/vpsops.tar.gz" "$ARCHIVE" || fail '下载 vpsops 源码失败'
    tar -xzf "$TMP_DIR/vpsops.tar.gz" -C "$TMP_DIR" || fail '解压 vpsops 源码失败'
    [ -f "$TMP_DIR/vpsops-master/deploy/vps/install.sh" ] || fail '源码包中缺少 VPS 安装脚本'
}

validate_agent_arguments() {
    [ "$#" -eq 3 ] || fail 'agent 模式需要 SSH 公钥和跳板机 IPv4 地址'
    PUBKEY=$2
    JUMP_IP=$3
    [ -n "$PUBKEY" ] || fail 'SSH 公钥不能为空'
    [ -n "$JUMP_IP" ] || fail '跳板机 IPv4 地址不能为空'

    NL='
'
    CR=$(printf '\r')
    case "$PUBKEY" in
        *"$NL"*|*"$CR"*) fail 'SSH 公钥必须是单行文本' ;;
        ssh-ed25519\ *) ;;
        *) fail '只接受以 ssh-ed25519 开头的公钥' ;;
    esac
    if printf '%s' "$PUBKEY" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        fail 'SSH 公钥不能包含控制字符'
    fi
    case "$JUMP_IP" in
        *[!0-9.]*|'') fail '跳板机地址只能包含 IPv4 数字和点号' ;;
    esac
    printf '%s\n' "$JUMP_IP" | LC_ALL=C grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' ||
        fail '跳板机地址必须是 IPv4 点分地址'
    saved_ifs=$IFS
    IFS=.
    set -- $JUMP_IP
    IFS=$saved_ifs
    for octet do
        [ "$octet" -le 255 ] || fail '跳板机 IPv4 地址的每段必须在 0 到 255 之间'
    done
}

install_agent() {
    require_root
    validate_agent_arguments "$@"
    require_command apt-get

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl tar python3 python3-yaml sudo openssh-server

    prepare_tmp_source
    sh "$TMP_DIR/vpsops-master/deploy/vps/install.sh" "$PUBKEY" "$JUMP_IP"
}

ensure_repository() {
    if [ -e "$REPO_DIR" ] || [ -L "$REPO_DIR" ]; then
        [ -d "$REPO_DIR" ] || fail "$REPO_DIR 已存在但不是目录，拒绝覆盖"
        [ ! -L "$REPO_DIR" ] || fail "$REPO_DIR 是符号链接，拒绝更新"
        git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
            fail "$REPO_DIR 已存在但不是 Git 仓库，拒绝覆盖"

        origin=$(git -C "$REPO_DIR" config --get remote.origin.url) || fail '现有仓库没有 origin'
        case "$origin" in
            https://github.com/Saito-Sakuya/vpsops.git|https://github.com/Saito-Sakuya/vpsops|\
            git@github.com:Saito-Sakuya/vpsops.git|ssh://git@github.com/Saito-Sakuya/vpsops.git) ;;
            *) fail "现有仓库 origin 不是官方仓库：$origin" ;;
        esac
        branch=$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD) ||
            fail '现有仓库处于 detached HEAD，请先切换到 master 分支'
        [ "$branch" = master ] || fail "现有仓库当前分支是 $branch；请先切换到 master 分支"
        git -C "$REPO_DIR" status --porcelain --untracked-files=all >/dev/null || fail '无法检查现有仓库状态'
        [ -z "$(git -C "$REPO_DIR" status --porcelain --untracked-files=all)" ] ||
            fail '现有仓库工作树不干净；请先提交或移走本地改动'
        git -C "$REPO_DIR" fetch origin master || fail '无法获取官方 master 分支'
        git -C "$REPO_DIR" merge --ff-only FETCH_HEAD || fail '现有仓库无法快进更新到官方 master'
    else
        git clone --branch master --single-branch "$REPOSITORY" "$REPO_DIR" || fail '克隆官方仓库失败'
    fi
}

install_gateway() {
    require_root
    gateway_prerequisites

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl git openssh-client python3 python3-venv sudo

    /usr/bin/python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' ||
        fail '系统 Python 必须为 3.11 或更新版本'
    ensure_repository

    if command -v uv >/dev/null 2>&1; then
        UV=$(command -v uv)
    elif [ -x /usr/local/bin/uv ]; then
        UV=/usr/local/bin/uv
    else
        make_tmp_dir
        curl -fLsS --output "$TMP_DIR/uv-install.sh" https://astral.sh/uv/install.sh ||
            fail '下载 uv 官方安装器失败'
        UV_INSTALL_DIR=/usr/local/bin sh "$TMP_DIR/uv-install.sh" || fail 'uv 安装失败'
        [ -x /usr/local/bin/uv ] || fail 'uv 安装器未生成 /usr/local/bin/uv'
        UV=/usr/local/bin/uv
    fi
    (cd "$REPO_DIR" && "$UV" sync --locked --no-dev --extra gateway --python /usr/bin/python3) ||
        fail 'uv 依赖安装失败'

    if ! getent group vpsops >/dev/null 2>&1; then
        groupadd --system vpsops
    fi
    if ! id vpsops >/dev/null 2>&1; then
        useradd --system --gid vpsops --home-dir "$REPO_DIR" --shell /usr/sbin/nologin vpsops
    fi
    install -d -o root -g vpsops -m 0750 "$CONFIG_DIR"

    PRIVATE_KEY=$CONFIG_DIR/id_ed25519
    PUBLIC_KEY=$PRIVATE_KEY.pub
    if [ ! -e "$PRIVATE_KEY" ] && [ ! -e "$PUBLIC_KEY" ]; then
        ssh-keygen -t ed25519 -N '' -C vpsops-gateway -f "$PRIVATE_KEY"
    elif [ ! -f "$PRIVATE_KEY" ] || [ ! -f "$PUBLIC_KEY" ]; then
        fail "SSH key 文件不完整；为保护现有文件，未覆盖：$PRIVATE_KEY"
    fi
    chown root:vpsops "$PRIVATE_KEY" "$PUBLIC_KEY"
    chmod 0640 "$PRIVATE_KEY"
    chmod 0644 "$PUBLIC_KEY"

    if [ ! -e "$CONFIG_DIR/gateway.yaml" ]; then
        install -o root -g vpsops -m 0640 \
            "$REPO_DIR/deploy/gateway/gateway.example.yaml" "$CONFIG_DIR/gateway.yaml"
    fi
    if [ ! -e "$CONFIG_DIR/tg.token" ]; then
        install -o root -g vpsops -m 0640 /dev/null "$CONFIG_DIR/tg.token"
    fi
    if [ ! -e "$CONFIG_DIR/known_hosts" ]; then
        install -o root -g vpsops -m 0640 /dev/null "$CONFIG_DIR/known_hosts"
    fi

    install -o root -g root -m 0644 \
        "$REPO_DIR/deploy/gateway/vpsops-gateway.service" \
        /etc/systemd/system/vpsops-gateway.service
    systemctl daemon-reload

    cat <<EOF
Gateway 已安装，当前未启动服务。

后续需要人工完成：
1. 生成客户端 token：$REPO_DIR/.venv/bin/vpsops-gateway token new <客户端名称>；将 token_sha256 填入 $CONFIG_DIR/gateway.yaml。
2. 编辑 $CONFIG_DIR/gateway.yaml，填写实际 Tailscale 监听地址、VPS 主机、客户端权限和 Telegram chat_id；把 Bot token 安全写入 $CONFIG_DIR/tg.token。
3. 核对 VPS SSH 主机指纹后写入 $CONFIG_DIR/known_hosts。
4. 在 Tailscale 管理后台启用 HTTPS 证书，并运行：tailscale cert --cert-file=$CONFIG_DIR/tls.crt --key-file=$CONFIG_DIR/tls.key <实际的-tailnet-域名>
5. 检查证书与配置后运行：
   chown root:vpsops $CONFIG_DIR/tls.crt $CONFIG_DIR/tls.key
   chmod 0640 $CONFIG_DIR/tls.crt $CONFIG_DIR/tls.key
   sudo -u vpsops $REPO_DIR/.venv/bin/vpsops-gateway check -c $CONFIG_DIR/gateway.yaml
   systemctl enable --now vpsops-gateway

跳板机 SSH 公钥：$PUBLIC_KEY
EOF
}

if [ "${1-}" = --help ]; then
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    usage
    exit 0
fi

case "${1-}" in
    gateway)
        [ "$#" -eq 1 ] || { usage >&2; exit 2; }
        install_gateway
        ;;
    agent)
        install_agent "$@"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
