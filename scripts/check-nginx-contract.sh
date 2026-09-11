#!/bin/sh
set -eu

nginx_image=$(awk 'index($0, "image: docker.io/library/nginx@sha256:") { print $2 }' compose.yaml)
production_image=$(awk 'index($0, "image: docker.io/library/nginx@sha256:") { print $2 }' compose.production.yaml)
test -n "$nginx_image" && test "$nginx_image" = "$production_image"

docker run --rm \
  --add-host api:127.0.0.1 \
  --add-host web:127.0.0.1 \
  -v "$PWD/infra/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$PWD/infra/nginx/conf.d:/etc/nginx/conf.d:ro" \
  "$nginx_image" nginx -t

config_file="infra/nginx/conf.d/default.conf"
grep -Fq 'resolver 127.0.0.11 valid=2s ipv6=off;' infra/nginx/nginx.conf
grep -Fq 'server api:8000 resolve;' "$config_file"
grep -Fq 'server web:3000 resolve;' "$config_file"
grep -Fq 'proxy_pass http://api_upstream/health/ready;' "$config_file"
grep -Fq 'proxy_pass http://web_upstream/zh-hant/login;' "$config_file"
grep -q 'proxy_buffering off;' "$config_file"
grep -q 'X-Accel-Buffering "no"' "$config_file"
grep -q 'location ~ \^/api/podcasts/' "$config_file"
grep -q 'location \^~ /api/v1/chat/stream' "$config_file"
grep -q 'client_max_body_size 16k;' "$config_file"
grep -q 'location = /api/admin/podcasts/uploads' "$config_file"
grep -q 'client_max_body_size 800m;' "$config_file"
grep -q 'proxy_set_header Host $http_host;' "$config_file"
grep -q 'proxy_set_header X-Forwarded-Host $http_host;' "$config_file"

if grep -q 'proxy_set_header X-Forwarded-Host $host;' "$config_file"
then
  echo "nginx 必須保留原始 Host port，否則 CSRF same-origin 檢查會誤判。" >&2
  exit 1
fi

if grep -Eq 'proxy_pass .*r2|R2_(ACCESS|SECRET|ACCOUNT)' infra/nginx/*.conf infra/nginx/conf.d/*.conf
then
  echo "nginx 不得代理 R2 bytes 或包含 R2 credentials。" >&2
  exit 1
fi
