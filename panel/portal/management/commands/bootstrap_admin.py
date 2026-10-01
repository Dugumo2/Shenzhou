"""只在受控终端初始化管理员，将一次性口令写入受限文件。"""
import os
import secrets
from pathlib import Path
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from portal.models import Membership


class Command(BaseCommand):
    help = '创建首个管理员；不会向终端打印密码'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)
        parser.add_argument('--username', default='megabox-admin')

    def handle(self, *args, **options):
        if User.objects.filter(is_staff=True).exists():
            raise CommandError('已有管理员；拒绝覆盖密码。')
        path = Path(options['output']).resolve()
        if not path.parent.is_dir() or path.exists():
            raise CommandError('输出目录须已存在，文件不能已存在。')
        # Windows ACL 需由交付脚本设置，不能把 POSIX mode 当作 ACL。
        if os.name != 'posix':
            raise CommandError('初始生产口令仅在 Linux 受限目录生成；本地测试使用测试账号。')
        if path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o077:
            raise CommandError('输出目录必须仅运行账户可访问。')
        password = secrets.token_urlsafe(24)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with transaction.atomic():
                user = User.objects.create_user(options['username'], password=password, is_staff=True, is_superuser=False)
                Membership.objects.create(user=user)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write('账号：' + user.username + '\n初始密码：' + password + '\n请登录后修改。\n')
                    stream.flush()
                    os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise CommandError('初始化失败，未交付凭据。')
        self.stdout.write('管理员已建立；初始凭据保存在指定受限文件，未输出到日志。')
