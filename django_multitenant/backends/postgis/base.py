import logging

import django
from django.db.backends.base.base import NO_DB_ALIAS
from django.contrib.gis.db.backends.postgis.schema import PostGISSchemaEditor as BasePostGISSchemaEditor
from django.contrib.gis.db.backends.postgis.base import (
    DatabaseFeatures as PostGISDatabaseFeatures,
    DatabaseWrapper as PostGISDatabaseWrapper,
    PostGISIntrospection,
    PostGISOperations,
)
from django_multitenant.fields import TenantForeignKey
from django_multitenant.utils import get_model_by_db_table, get_tenant_column

logger = logging.getLogger(__name__)


class PostGISSchemaEditor(BasePostGISSchemaEditor):
    sql_create_column_inline_fk = None

    # Override
    def __enter__(self):
        ret = super(PostGISSchemaEditor, self).__enter__()
        return ret

    # Override
    def _alter_field(self, model, old_field, new_field, old_type, new_type, old_db_params, new_db_params, strict=False):

        super(PostGISSchemaEditor, self)._alter_field(
            model, old_field, new_field, old_type, new_type, old_db_params, new_db_params, strict
        )

        # If the pkey was dropped in the previous distribute migration,
        # foreign key constraints didn't previously exists so django does not
        # recreated them.
        # Here we test if we are in this case
        if isinstance(new_field, TenantForeignKey) and new_field.db_constraint:
            from_model = get_model_by_db_table(model._meta.db_table)
            fk_names = self._constraint_names(model, [new_field.column], foreign_key=True) + self._constraint_names(
                model,
                [new_field.column, get_tenant_column(from_model)],
                foreign_key=True,
            )
            if not fk_names:
                self.execute(self._create_fk_sql(model, new_field, "_fk_%(to_table)s_%(to_column)s"))

    # Override
    def _create_fk_sql(self, model, field, suffix):
        if isinstance(field, TenantForeignKey):
            try:
                # test if both models exists
                # This case happens when we are running from scratch migrations and one model was removed from code
                # In the previous migrations we would still be creating the foreign key
                from_model = get_model_by_db_table(model._meta.db_table)
                to_model = get_model_by_db_table(field.target_field.model._meta.db_table)
            except ValueError:
                return None

            from_columns = field.column, get_tenant_column(from_model)
            to_columns = field.target_field.column, get_tenant_column(to_model)
            suffix = suffix % {
                "to_table": field.target_field.model._meta.db_table,
                "to_column": "_".join(to_columns),
            }

            return self.sql_create_fk % {
                "table": self.quote_name(model._meta.db_table),
                "name": self.quote_name(self._create_index_name(model._meta.db_table, from_columns, suffix=suffix)),
                "column": ", ".join([self.quote_name(from_col) for from_col in from_columns]),
                "to_table": self.quote_name(field.target_field.model._meta.db_table),
                "to_column": ", ".join([self.quote_name(to_col) for to_col in to_columns]),
                "deferrable": self.connection.ops.deferrable_sql(),
            }
        return super(PostGISSchemaEditor, self)._create_fk_sql(model, field, suffix)

    # Override
    def execute(self, sql, params=()):
        # Hack: Citus will throw the following error if these statements are
        # not executed separately: "ERROR: cannot execute multiple utility events"
        if sql and not params:
            for statement in str(sql).split(";"):
                if statement and not statement.isspace():
                    super(PostGISSchemaEditor, self).execute(statement)
        elif sql:
            super(PostGISSchemaEditor, self).execute(sql, params)

    def _create_index_name(self, model, column_names, suffix=""):
        # compat with django 2.X and django 1.X
        import django

        if not isinstance(model, str) and django.VERSION[0] > 1:
            model = model._meta.db_table

        return super(PostGISSchemaEditor, self)._create_index_name(model, column_names, suffix=suffix)


class DatabaseFeatures(PostGISDatabaseFeatures):
    # The default Django behaviour is to collapse the fields to just the 'id'
    # field. This doesn't work because we're using a composite primary key. In
    # Django version 3.0 a function was added that we can override to specify
    # for specific models that this behaviour should be disabled.
    def allows_group_by_selected_pks_on_model(self, model):
        from django_multitenant.models import TenantModel

        if issubclass(model, TenantModel):
            return False
        return super().allows_group_by_selected_pks_on_model(model)

    # For django versions before version 3.0 we set a flag that disables this
    # behaviour for all models.
    if django.VERSION < (3, 0):
        allows_group_by_selected_pks = False


class DatabaseWrapper(PostGISDatabaseWrapper):
    # Override
    SchemaEditorClass = PostGISSchemaEditor
    features_class = DatabaseFeatures
    introspection_class = PostGISIntrospection
    ops_class = PostGISOperations

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if kwargs.get('alias', '') != NO_DB_ALIAS:
            self.features = DatabaseFeatures(self)
            self.ops = PostGISOperations(self)
            self.introspection = PostGISIntrospection(self)
