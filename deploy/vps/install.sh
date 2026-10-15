#!/bin/sh
# 在 VPS 上以 root 运行：sh install.sh "<gateway 公钥>" <跳板机IP>
# 例：sh install.sh "ssh-ed25519 AAAA... vpsops-gateway" 100.64.0.5
set -eu
PUBKEY=$1
JUMP_IP=$2
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "需要 root"; exit 1; }
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }
python3 -c 'import yaml' 2>/dev/null || echo "提示：未安装 python3-yaml，将只能使用 /etc/ai-agent.json"

id ai-ops >/dev/null 2>&1 || useradd --system --create-home --shell /bin/sh ai-ops
passwd -l ai-ops >/dev/null

install -o root -g root -m 755 "$HERE/../../agent/ai_agent.py" /usr/local/bin/ai-agent
install -d -o root -g root -m 700 /var/lib/ai-agent

install -d -o root -g ai-ops -m 750 /home/ai-ops/.ssh   # 归 root：ai-ops 无法替换 authorized_keys
echo "restrict,from=\"$JUMP_IP\",command=\"sudo -n /usr/local/bin/ai-agent\" $PUBKEY" > /home/ai-ops/.ssh/authorized_keys
chown root:ai-ops /home/ai-ops/.ssh/authorized_keys
chmod 640 /home/ai-ops/.ssh/authorized_keys   # 归 root：ai-ops 自己改不了

cp "$HERE/sudoers.ai-ops" /tmp/ai-ops.sudoers
visudo -cf /tmp/ai-ops.sudoers
install -o root -g root -m 440 /tmp/ai-ops.sudoers /etc/sudoers.d/ai-ops
rm /tmp/ai-ops.sudoers

if [ -d /etc/ssh/sshd_config.d ]; then
    install -o root -g root -m 644 "$HERE/sshd-ai-ops.conf" /etc/ssh/sshd_config.d/ai-ops.conf
    sshd -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd)
else
    echo "提示：没有 /etc/ssh/sshd_config.d，请手动把 sshd-ai-ops.conf 追加到 sshd_config"
fi

if [ ! -f /etc/ai-agent.yaml ]; then
    install -o root -g root -m 600 "$HERE/ai-agent.example.yaml" /etc/ai-agent.yaml
    echo "已放入示例 /etc/ai-agent.yaml —— 请按本机实际情况修改"
fi

echo "完成。在跳板机上测试：echo '{\"op\":\"describe\"}' | ssh -i <key> ai-ops@<本机IP>"
echo "别忘了：防火墙只允许 $JUMP_IP 访问 22 端口"
