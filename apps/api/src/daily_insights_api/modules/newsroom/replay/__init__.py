"""Offline replay of past newsroom editions (selection P0, task 0).

A replay copies a date range out of the production database read-only, loads it
into a throwaway pgvector database, and runs the real triage and assembly code
there under a chosen ``SelectionVariant``. Model calls go through a response
cache, Slack is never configured, and the results are written as JSON and a
Markdown summary outside the repository.
"""
