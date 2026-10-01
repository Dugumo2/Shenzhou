"""管理命令入口；本地默认仅为候选环境。"""
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'megabox.settings')
from django.core.management import execute_from_command_line

execute_from_command_line(sys.argv)
