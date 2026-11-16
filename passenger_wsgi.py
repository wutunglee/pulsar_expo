"""Phusion Passenger 的進入點（與 exercise-ai 相同的部署方式）。"""
from app import create_app

application = create_app()
