"""套餐只读查询的同次数据库快照；不申请SQLite写预留锁。"""
from contextlib import contextmanager

from django.db import connection, transaction


@contextmanager
def read_snapshot():
    connection.ensure_connection()
    prior = getattr(connection, 'transaction_mode', None)
    change = connection.vendor == 'sqlite' and not connection.in_atomic_block
    if change:
        connection.transaction_mode = 'DEFERRED'
    try:
        with transaction.atomic():
            yield
    finally:
        if change:
            connection.transaction_mode = prior
