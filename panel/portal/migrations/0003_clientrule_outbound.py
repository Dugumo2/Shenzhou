from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('portal', '0002_clientdirectrule')]

    operations = [
        migrations.AddField(
            model_name='clientdirectrule',
            name='outbound',
            field=models.CharField(choices=[('proxy', '当前代理'), ('direct', '本机直连')], default='direct', max_length=8),
        ),
    ]
