"""新增默认关闭的 P8 本人来源绑定；不迁入数据或消费真实秘密。"""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0014_rule_sources'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='P8SourceBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('source_instance', models.CharField(max_length=96)),
                ('source_id', models.CharField(max_length=96)),
                ('enabled', models.BooleanField(default=False)),
                ('state', models.CharField(choices=[('unverified', '未核验'), ('verified', '已核验'), ('revoked', '已撤销')], default='unverified', max_length=12)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('evidence_sha256', models.CharField(max_length=64)),
                ('links_sha256', models.CharField(max_length=64)),
                ('verification_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='p8_source_bindings', to=settings.AUTH_USER_MODEL)),
                ('legacy_membership', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='p8_binding', to='portal.membership')),
                ('verified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='verified_p8_bindings', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('source_instance', 'source_id'), name='unique_p8_source_binding'), models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='p8_binding_positive_revision'), models.CheckConstraint(condition=models.Q(('state__in', ['unverified', 'verified', 'revoked'])), name='p8_binding_known_state')],
            },
        ),
    ]
