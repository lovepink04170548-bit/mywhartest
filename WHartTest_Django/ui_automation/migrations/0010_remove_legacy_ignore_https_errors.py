from django.db import migrations


TABLE_NAME = 'ui_environment_config'
LEGACY_COLUMN = 'ignore_https_errors'


def remove_legacy_ignore_https_errors(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, TABLE_NAME)
        }

    if LEGACY_COLUMN not in columns:
        return

    environment_config = apps.get_model('ui_automation', 'UiEnvironmentConfig')
    quoted_table = schema_editor.quote_name(TABLE_NAME)
    quoted_column = schema_editor.quote_name(LEGACY_COLUMN)

    with connection.cursor() as cursor:
        cursor.execute(f'SELECT id, {quoted_column} FROM {quoted_table}')
        legacy_values = cursor.fetchall()

    for config_id, ignore_https_errors in legacy_values:
        config = environment_config.objects.filter(pk=config_id).first()
        if config is None:
            continue
        extra_config = config.extra_config if isinstance(config.extra_config, dict) else {}
        if 'ignore_https_errors' not in extra_config:
            extra_config = {
                **extra_config,
                'ignore_https_errors': bool(ignore_https_errors),
            }
            environment_config.objects.filter(pk=config_id).update(extra_config=extra_config)

    schema_editor.execute(
        f'ALTER TABLE {quoted_table} DROP COLUMN {quoted_column}'
    )


class Migration(migrations.Migration):

    dependencies = [
        ('ui_automation', '0009_element_map_version_governance'),
    ]

    operations = [
        migrations.RunPython(
            remove_legacy_ignore_https_errors,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
