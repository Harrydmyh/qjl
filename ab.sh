#!/bin/bash
# agent-browser 固定参数包装器
# 作用：所有调用参数完全一致，避免 daemon 检测到参数变化而重启浏览器（重启会丢登录态）
# 用法：./ab.sh open "https://..."   /  ./ab.sh screenshot /tmp/x.png
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY NO_PROXY
exec agent-browser \
  --headed \
  --profile /tmp/qjl-browser-profile \
  --restore qjlwecom \
  --proxy-bypass "127.0.0.1,localhost,::1" \
  "$@"
