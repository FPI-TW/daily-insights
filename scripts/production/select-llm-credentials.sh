#!/bin/sh
# Sourced by release validation. Never use a truthy fallback for an OpenAI key:
# an empty native key must fail instead of sending a legacy key to OpenAI.
case "${DAILY_INSIGHTS_CHAT_MODEL_PROVIDER:-}" in
  openai)
    if [ "${DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL:-}" != https://api.openai.com/v1 ]; then
      echo "openai chat requires https://api.openai.com/v1" >&2
      return 1
    fi
    DAILY_INSIGHTS_CHAT_MODEL_API_KEY=${DAILY_INSIGHTS_CHAT_OPENAI_API_KEY:-}
    if [ "${DAILY_INSIGHTS_CHAT_ENABLED:-false}" = true ] && [ -z "$DAILY_INSIGHTS_CHAT_MODEL_API_KEY" ]; then
      echo "DAILY_INSIGHTS_CHAT_OPENAI_API_KEY is required for OpenAI chat" >&2
      return 1
    fi
    ;;
esac
case "${DAILY_INSIGHTS_NEWSROOM_LLM_BASE_URL:-https://api.openai.com/v1}" in
  https://api.openai.com/v1)
    DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY=${DAILY_INSIGHTS_NEWSROOM_OPENAI_API_KEY:-}
    if [ "${DAILY_INSIGHTS_NEWSROOM_ENABLED:-false}" = true ] && [ -z "$DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY" ]; then
      echo "DAILY_INSIGHTS_NEWSROOM_OPENAI_API_KEY is required for the OpenAI newsroom" >&2
      return 1
    fi
    ;;
esac
export DAILY_INSIGHTS_CHAT_MODEL_API_KEY DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY
