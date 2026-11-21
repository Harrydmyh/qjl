#!/bin/bash
# 系统代理开关（macOS）
# 用法：
#   ./proxy.sh status    看当前状态
#   ./proxy.sh off       全部关掉，所有流量直连
#   ./proxy.sh on        恢复原来的 Veee 代理（HTTP/HTTPS 15236，SOCKS 15235）
#
# 注意：这台机器上还有 WorkBuddy 自己的内部通道 127.0.0.1:57890（sandbox-c 进程），
# 它不在系统代理里，本脚本不会碰它，也不该碰——关掉它工具链会断。

set -u

PROXY_HOST="127.0.0.1"
HTTP_PORT="15236"
SOCKS_PORT="15235"
SERVICES=("Wi-Fi" "Thunderbolt Bridge" "iPhone USB")

cmd="${1:-status}"

case "$cmd" in
  off)
    for svc in "${SERVICES[@]}"; do
      networksetup -setwebproxystate "$svc" off
      networksetup -setsecurewebproxystate "$svc" off
      networksetup -setsocksfirewallproxystate "$svc" off
      echo "已关闭 $svc 的代理"
    done
    ;;
  on)
    for svc in "${SERVICES[@]}"; do
      networksetup -setwebproxy "$svc" "$PROXY_HOST" "$HTTP_PORT" off
      networksetup -setsecurewebproxy "$svc" "$PROXY_HOST" "$HTTP_PORT" off
      networksetup -setsocksfirewallproxy "$svc" "$PROXY_HOST" "$SOCKS_PORT" off
      networksetup -setwebproxystate "$svc" on
      networksetup -setsecurewebproxystate "$svc" on
      networksetup -setsocksfirewallproxystate "$svc" on
      echo "已恢复 $svc 的代理 -> $PROXY_HOST:$HTTP_PORT / socks $SOCKS_PORT"
    done
    ;;
  status)
    echo "=== 系统代理 ==="
    scutil --proxy | grep -E "HTTPEnable|HTTPSEnable|SOCKSEnable|HTTPPort|HTTPSPort|SOCKSPort|Proxy :"
    echo
    echo "=== 代理客户端进程 ==="
    lsof -nP -iTCP:$HTTP_PORT -sTCP:LISTEN 2>/dev/null | tail -n +2 | awk '{print "  监听 " $9 " -> " $1 " (PID " $2 ")"}' || true
    echo
    echo "=== 直连可达性 ==="
    curl -s -o /dev/null -w "  qyapi.weixin.qq.com  %{http_code}  %{time_total}s\n" --noproxy '*' --max-time 8 https://qyapi.weixin.qq.com/cgi-bin/gettoken
    ;;
  *)
    echo "用法: $0 {status|off|on}"
    exit 1
    ;;
esac
