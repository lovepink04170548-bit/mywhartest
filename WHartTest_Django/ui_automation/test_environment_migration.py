import importlib

from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase

from projects.models import Project
from ui_automation.models import UiEnvironmentConfig


legacy_migration = importlib.import_module(
    'ui_automation.migrations.0010_remove_legacy_ignore_https_errors'
)


class LegacyIgnoreHttpsErrorsMigrationTests(TransactionTestCase):
    def test_legacy_column_is_migrated_to_extra_config_and_removed(self):
        user = User.objects.create_user(username='env-migration-user')
        project = Project.objects.create(name='Environment Migration Project', creator=user)
        inherited = UiEnvironmentConfig.objects.create(
            project=project,
            creator=user,
            name='Inherited legacy setting',
            extra_config={},
        )
        explicit = UiEnvironmentConfig.objects.create(
            project=project,
            creator=user,
            name='Explicit current setting',
            extra_config={'ignore_https_errors': False},
        )

        with connection.schema_editor() as schema_editor:
            schema_editor.execute(
                'ALTER TABLE ui_environment_config '
                'ADD COLUMN ignore_https_errors BOOLEAN NOT NULL DEFAULT TRUE'
            )
            legacy_migration.remove_legacy_ignore_https_errors(apps, schema_editor)

        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, 'ui_environment_config'
                )
            }

        inherited.refresh_from_db()
        explicit.refresh_from_db()
        self.assertNotIn('ignore_https_errors', columns)
        self.assertIs(inherited.extra_config['ignore_https_errors'], True)
        self.assertIs(explicit.extra_config['ignore_https_errors'], False)
