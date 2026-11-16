"""gunicorn 等 WSGI 伺服器的進入點：gunicorn wsgi:application"""
from app import create_app

application = create_app()
