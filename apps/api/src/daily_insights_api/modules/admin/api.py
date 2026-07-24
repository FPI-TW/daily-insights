"""Privileged workflow boundary.

The HTTP router is an application composition root. Domain modules expose
their own public interfaces; admin is allowed to coordinate them inside one
database transaction but does not own their tables.
"""
