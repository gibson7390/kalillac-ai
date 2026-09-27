"""Kalillac database package.

This package must remain safe to import when PostgreSQL is disabled or
unavailable. Importing it must not create an engine or open a connection.
"""
