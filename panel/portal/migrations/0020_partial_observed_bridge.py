# Django 5.2.17生成于2026-10-13；仅增观测窗口与导入水位，不回填或清空旧账。

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0019_ingress_metering'),
    ]

    operations = [
        migrations.AddField(
            model_name='ingressusagesample',
            name='observation_verified',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='ingressusagesample',
            name='read_windows',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='ingressusagesample',
            name='source_config_fingerprint',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='usageledger',
            name='ingress_sample',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='portal.ingressusagesample'),
        ),
        migrations.CreateModel(
            name='JournalImportCheckpoint',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bridge_ref', models.CharField(max_length=80, unique=True)),
                ('observer_fingerprint', models.CharField(max_length=64)),
                ('mapping_fingerprint', models.CharField(max_length=64)),
                ('last_sequence', models.PositiveBigIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('entitlement', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.entitlement')),
            ],
        ),
        migrations.CreateModel(
            name='JournalImportReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_sequence', models.PositiveBigIntegerField()),
                ('source_fingerprint', models.CharField(max_length=64)),
                ('result', models.CharField(max_length=16)),
                ('sample_ids', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('checkpoint', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.journalimportcheckpoint')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('checkpoint', 'source_sequence'), name='unique_journal_import_receipt')],
            },
        ),
    ]
