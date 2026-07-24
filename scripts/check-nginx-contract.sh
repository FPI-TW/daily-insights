#!/bin/sh
set -eu

docker run --rm \
  --add-host api:127.0.0.1 \
  --add-host web:127.0.0.1 \
  -v "$PWD/infra/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$PWD/infra/nginx/conf.d:/etc/nginx/conf.d:ro" \
  nginx:1.27-alpine nginx -t

config_file="infra/nginx/conf.d/default.conf"
grep -q 'proxy_buffering off;' "$config_file"
grep -q 'X-Accel-Buffering "no"' "$config_file"
grep -q 'location ~ \^/api/podcasts/' "$config_file"
grep -q 'client_max_body_size 16k;' "$config_file"

if grep -Eq 'proxy_pass .*r2|R2_(ACCESS|SECRET|ACCOUNT)' infra/nginx/*.conf infra/nginx/conf.d/*.conf
then
  echo "nginx 不得代理 R2 bytes 或包含 R2 credentials。" >&2
  exit 1
fi
