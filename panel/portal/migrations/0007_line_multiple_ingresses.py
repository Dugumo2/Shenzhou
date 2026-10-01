"""线路包含多个协议入口；旧候选身份明确绑定原主入口，不修改其凭据引用。"""
import django.db.models.deletion
from django.db import migrations, models


def bind_existing(apps, schema_editor):
    Identity = apps.get_model('portal', 'NodeIdentity')
    for identity in Identity.objects.select_related('line').iterator():
        identity.ingress_id = identity.line.ingress_id
        identity.save(update_fields=['ingress'])


class Migration(migrations.Migration):
    dependencies = [('portal', '0006_phase1_enforcement_epoch')]
    operations = [
        migrations.AddField(model_name='line', name='additional_ingresses',
            field=models.ManyToManyField(blank=True, related_name='additional_lines', to='portal.ingress')),
        migrations.AddField(model_name='nodeidentity', name='ingress',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='portal.ingress')),
        migrations.RunPython(bind_existing, migrations.RunPython.noop),
        migrations.AlterField(model_name='nodeidentity', name='ingress',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.ingress')),
        migrations.RemoveConstraint(model_name='nodeidentity', name='unique_identity_generation'),
        migrations.AddConstraint(model_name='nodeidentity', constraint=models.UniqueConstraint(
            fields=('subscription', 'line', 'ingress', 'generation'), name='unique_identity_generation')),
    ]
