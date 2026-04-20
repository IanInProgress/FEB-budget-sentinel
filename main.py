"""
Entry point for gunicorn when Railway (or other platforms) use the default
start command: gunicorn main:app
"""
from app import server

app = server
