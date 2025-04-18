import importlib
import json
import re
from collections import defaultdict, namedtuple
from logging import getLogger
from typing import Any, Optional
from pathlib import Path

import sqlalchemy as sa
from packaging.version import Version
from sqlalchemy import inspect
from sqlalchemy import util
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, ENUM
from sqlalchemy.dialects.postgresql.base import (PGCompiler, PGDDLCompiler,
                                                 PGDialect, PGExecutionContext,
                                                 PGIdentifierPreparer,
                                                 PGTypeCompiler)
from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2
from sqlalchemy.dialects.postgresql.psycopg2cffi import PGDialect_psycopg2cffi
from sqlalchemy.engine import reflection
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import (BinaryExpression, BooleanClauseList,
                                       Delete)
from sqlalchemy.sql.type_api import TypeEngine
from sqlalchemy.sql import sqltypes
from sqlalchemy.types import (BIGINT, BOOLEAN, CHAR, DATE, DECIMAL, INTEGER,
                              REAL, SMALLINT, TIMESTAMP, VARCHAR, NullType)

from .commands import (AlterTableAppendCommand, Compression, CopyCommand,
                       CreateLibraryCommand, Encoding, Format,
                       RefreshMaterializedView, UnloadFromSelect)
from .ddl import (CreateMaterializedView, DropMaterializedView,
                  get_table_attributes)
from typing import List

sa_version = Version(sa.__version__)
logger = getLogger(__name__)

try:
    import alembic
except ImportError:
    pass
else:
    from alembic.ddl import postgresql
    from alembic.ddl.base import RenameTable
    compiles(RenameTable, 'redshift')(postgresql.visit_rename_table)

    if Version(alembic.__version__) >= Version('1.0.6'):
        from alembic.ddl.base import ColumnComment
        compiles(ColumnComment, 'redshift')(postgresql.visit_column_comment)

    class RedshiftImpl(postgresql.PostgresqlImpl):
        __dialect__ = 'redshift'

# "Each dialect provides the full set of typenames supported by that backend
# with its __all__ collection
# https://docs.sqlalchemy.org/en/13/core/type_basics.html#vendor-specific-types
__all__ = (
    'SMALLINT',
    'INTEGER',
    'BIGINT',
    'DECIMAL',
    'REAL',
    'BOOLEAN',
    'CHAR',
    'DATE',
    'TIMESTAMP',
    'VARCHAR',
    'DOUBLE_PRECISION',
    'GEOMETRY',
    'SUPER',
    'TIMESTAMPTZ',
    'TIMETZ',
    'HLLSKETCH',

    'RedshiftDialect', 'RedshiftDialect_psycopg2',
    'RedshiftDialect_psycopg2cffi', 'RedshiftDialect_redshift_connector',

    'CopyCommand', 'UnloadFromSelect', 'Compression',
    'Encoding', 'Format', 'CreateLibraryCommand', 'AlterTableAppendCommand',
    'RefreshMaterializedView',

    'CreateMaterializedView', 'DropMaterializedView'
)


# Regex for parsing and identity constraint out of adsrc, e.g.:
#   "identity"(445178, 0, '1,1'::text)
IDENTITY_RE = re.compile(r"""
    "identity" \(
      (?P<current>-?\d+)
      ,\s
      (?P<base>-?\d+)
      ,\s
      '(?P<seed>-?\d+),(?P<step>-?\d+)'
      .*
    \)
""", re.VERBOSE)

# Regex for SQL identifiers (valid table and column names)
SQL_IDENTIFIER_RE = re.compile(r"""
   [_a-zA-Z][\w$]*  # SQL standard identifier
   |                # or
   (?:"[^"]+")+     # SQL delimited (quoted) identifier
""", re.VERBOSE)

# Regex for foreign key constraints, e.g.:
#   FOREIGN KEY(col1) REFERENCES othertable (col2)
# See https://docs.aws.amazon.com/redshift/latest/dg/r_names.html
# for a definition of valid SQL identifiers.
FOREIGN_KEY_RE = re.compile(r"""
  ^FOREIGN\ KEY \s* \(   # FOREIGN KEY, arbitrary whitespace, literal '('
    (?P<columns>         # Start a group to capture the referring columns
      (?:                # Start a non-capturing group
        \s*              # Arbitrary whitespace
        ([_a-zA-Z][\w$]* | ("[^"]+")+)   # SQL identifier
        \s*              # Arbitrary whitespace
        ,?               # There will be a colon if this isn't the last one
      )+                 # Close the non-capturing group; require at least one
    )                    # Close the 'columns' group
  \s* \)                 # Arbitrary whitespace and literal ')'
  \s* REFERENCES \s*
    ((?P<referred_schema>([_a-zA-Z][\w$]* | ("[^"]*")+))\.)? # SQL identifier
    (?P<referred_table>[_a-zA-Z][\w$]* | ("[^"]*")+)         # SQL identifier
  \s* \(   # FOREIGN KEY, arbitrary whitespace, literal '('
    (?P<referred_columns> # Start a group to capture the referring columns
      (?:                # Start a non-capturing group
        \s*              # Arbitrary whitespace
        ([_a-zA-Z][\w$]* | ("[^"]+")+)   # SQL identifier
        \s*              # Arbitrary whitespace
        ,?               # There will be a colon if this isn't the last one
      )+                 # Close the non-capturing group; require at least one
    )                    # Close the 'columns' group
  \s* \)                 # Arbitrary whitespace and literal ')'
""", re.VERBOSE)

# Regex for primary key constraints, e.g.:
#   PRIMARY KEY (col1, col2)
PRIMARY_KEY_RE = re.compile(r"""
  ^PRIMARY \s* KEY \s* \(  # FOREIGN KEY, arbitrary whitespace, literal '('
    (?P<columns>         # Start a group to capture column names
      (?:
        \s*                # Arbitrary whitespace
        # SQL identifier or delimited identifier
        ( [_a-zA-Z][\w$]* | ("[^"]*")+ )
        \s*                # Arbitrary whitespace
        ,?                 # There will be a colon if this isn't the last one
      )+                  # Close the non-capturing group; require at least one
    )
  \s* \) \s*                # Arbitrary whitespace and literal ')'
""", re.VERBOSE)

# Reserved words as extracted from Redshift docs.
# See pull_reserved_words.sh at the top level of this repository
# for the code used to generate this set.
RESERVED_WORDS = set([
    "aes128", "aes256", "all", "allowoverwrite", "analyse", "analyze",
    "and", "any", "array", "as", "asc", "authorization", "az64",
    "backup", "between", "binary", "blanksasnull", "both", "bytedict",
    "bzip2", "case", "cast", "check", "collate", "column", "constraint",
    "create", "credentials", "cross", "current_date", "current_time",
    "current_timestamp", "current_user", "current_user_id", "default",
    "deferrable", "deflate", "defrag", "delta", "delta32k", "desc",
    "disable", "distinct", "do", "else", "emptyasnull", "enable",
    "encode", "encrypt", "encryption", "end", "except", "explicit",
    "false", "for", "foreign", "freeze", "from", "full", "globaldict256",
    "globaldict64k", "grant", "group", "gzip", "having", "identity",
    "ignore", "ilike", "in", "initially", "inner", "intersect", "into",
    "is", "isnull", "join", "language", "leading", "left", "like",
    "limit", "localtime", "localtimestamp", "lun", "luns", "lzo", "lzop",
    "minus", "mostly16", "mostly32", "mostly8", "natural", "new", "not",
    "notnull", "null", "nulls", "off", "offline", "offset", "oid", "old",
    "on", "only", "open", "or", "order", "outer", "overlaps", "parallel",
    "partition", "percent", "permissions", "pivot", "placing", "primary",
    "raw", "readratio", "recover", "references", "respect", "rejectlog",
    "resort", "restore", "right", "select", "session_user", "similar",
    "snapshot", "some", "sysdate", "system", "table", "tag", "tdes",
    "text255", "text32k", "then", "timestamp", "to", "top", "trailing",
    "true", "truncatecolumns", "union", "unique", "unnest", "unpivot",
    "user", "using", "verbose", "wallet", "when", "where", "with",
    "without",
])

REFLECTION_SQL = """\
    SELECT
        n.nspname as "schema",
        c.relname as "table_name",
        att.attname as "name",
        format_encoding(att.attencodingtype::integer) as "encode",
        format_type(att.atttypid, att.atttypmod) as "type",
        att.attisdistkey as "distkey",
        att.attsortkeyord as "sortkey",
        att.attnotnull as "notnull",
        pg_catalog.col_description(att.attrelid, att.attnum)
        as "comment",
        adsrc,
        attnum,
        pg_catalog.format_type(att.atttypid, att.atttypmod),
        pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS DEFAULT,
        n.oid as "schema_oid",
        c.oid as "table_oid"
    FROM pg_catalog.pg_class c
    LEFT JOIN pg_catalog.pg_namespace n
        ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute att
        ON att.attrelid = c.oid
    LEFT JOIN pg_catalog.pg_attrdef ad
        ON (att.attrelid, att.attnum) = (ad.adrelid, ad.adnum)
    WHERE n.nspname !~ '^pg_'
        AND att.attnum > 0
        AND NOT att.attisdropped
        {schema_clause} {table_clause}
    UNION
    SELECT
        view_schema as "schema",
        view_name as "table_name",
        col_name as "name",
        null as "encode",
        col_type as "type",
        null as "distkey",
        0 as "sortkey",
        null as "notnull",
        null as "comment",
        null as "adsrc",
        null as "attnum",
        col_type as "format_type",
        null as "default",
        null as "schema_oid",
        null as "table_oid"
    FROM pg_get_late_binding_view_cols() cols(
        view_schema name,
        view_name name,
        col_name name,
        col_type varchar,
        col_num int)
    WHERE 1 {schema_clause} {table_clause}
    UNION
    SELECT c.schemaname AS "schema",
        c.tablename AS "table_name",
        c.columnname AS "name",
        null AS "encode",
        -- Spectrum represents data types differently.
        -- Standardize, so we can infer types.
        CASE
            WHEN c.external_type = 'int' THEN 'integer'
            WHEN c.external_type = 'float' THEN 'real'
            WHEN c.external_type = 'double' THEN 'double precision'
            WHEN c.external_type = 'timestamp'
            THEN 'timestamp without time zone'
            WHEN c.external_type ilike 'varchar%'
            THEN replace(c.external_type, 'varchar', 'character varying')
            WHEN c.external_type ilike 'decimal%'
            THEN replace(c.external_type, 'decimal', 'numeric')
            ELSE
            replace(
            replace(
                replace(c.external_type, 'decimal', 'numeric'),
                'char', 'character'),
            'varchar', 'character varying')
            END
            AS "type",
        false AS "distkey",
        0 AS "sortkey",
        null AS "notnull",
        null as "comment",
        null AS "adsrc",
        c.columnnum AS "attnum",
        CASE
            WHEN c.external_type = 'int' THEN 'integer'
            WHEN c.external_type = 'float' THEN 'real'
            WHEN c.external_type = 'double' THEN 'double precision'
            WHEN c.external_type = 'timestamp'
            THEN 'timestamp without time zone'
            WHEN c.external_type ilike 'varchar%'
            THEN replace(c.external_type, 'varchar', 'character varying')
            WHEN c.external_type ilike 'decimal%'
            THEN replace(c.external_type, 'decimal', 'numeric')
            ELSE
            replace(
            replace(
                replace(c.external_type, 'decimal', 'numeric'),
                'char', 'character'),
            'varchar', 'character varying')
            END
            AS "format_type",
        null AS "default",
        s.esoid AS "schema_oid",
        null AS "table_oid"
    FROM svv_external_columns c
    JOIN svv_external_schemas s ON s.schemaname = c.schemaname
    WHERE 1 {schema_clause} {table_clause}
    UNION
    SELECT c.database_name || '.' || c.schema_name     AS "schema",
       c.table_name AS "table_name",
       c.column_name AS "name",
       null         AS "encode",
       c.data_type  AS "type",
       false        AS "distkey",
       0            AS "sortkey",
       null         AS "notnull",
       null         AS "comment",
       null         AS "adsrc",
       c.ordinal_position AS "attnum",
       c.data_type  AS "format_type",
       null         AS "default",
       null         AS "schema_oid",
       null         AS "table_oid"
    FROM svv_redshift_columns c
             JOIN svv_redshift_tables t ON t.table_name = c.table_name AND t.schema_name = c.schema_name
    WHERE 1 {datashare_clause}{schema_clause} {unambiguous_tablename_clause}
    ORDER BY "schema", "table_name", "attnum";
    """


def parse_datashare(schema=None):
    """try and extract datashare from schema, if any

    returns datashare, schema, found_datashare"""
    if not schema:
        return None, schema, False
    has_datashare = "." in schema and '"' not in schema
    if not has_datashare:
        return None, schema, has_datashare
    datashare = schema.split(".")[0] if has_datashare else ""
    schema = schema.split(".")[1] if has_datashare else schema
    return datashare, schema, has_datashare

class RedshiftTypeEngine(TypeEngine):

    def _default_dialect(self, default=None):
        """
        Returns the default dialect used for TypeEngine compilation yielding
        String result.

        :meth:`~sqlalchemy.sql.type_api.TypeEngine.compile`
        """
        return RedshiftDialectMixin()


class TIMESTAMPTZ(RedshiftTypeEngine, sa.dialects.postgresql.TIMESTAMP):
    """
    Redshift defines a TIMTESTAMPTZ column type as an alias
    of TIMESTAMP WITH TIME ZONE.
    https://docs.aws.amazon.com/redshift/latest/dg/c_Supported_data_types.html

    Adding an explicit type to the RedshiftDialect allows us follow the
    SqlAlchemy conventions for "vendor-specific types."

    https://docs.sqlalchemy.org/en/13/core/type_basics.html#vendor-specific-types
    """

    __visit_name__ = 'TIMESTAMPTZ'

    def __init__(self, timezone=True, precision=None):
        # timezone param must be present as it's provided in base class so the
        # object can be instantiated with kwargs. see
        # :meth:`~sqlalchemy.dialects.postgresql.base.PGDialect._get_column_info`
        super(TIMESTAMPTZ, self).__init__(timezone=True, precision=precision)


class TIMETZ(RedshiftTypeEngine, sa.dialects.postgresql.TIME):
    """
    Redshift defines a TIMTETZ column type as an alias
    of TIME WITH TIME ZONE.
    https://docs.aws.amazon.com/redshift/latest/dg/c_Supported_data_types.html

    Adding an explicit type to the RedshiftDialect allows us follow the
    SqlAlchemy conventions for "vendor-specific types."

    https://docs.sqlalchemy.org/en/13/core/type_basics.html#vendor-specific-types
    """

    __visit_name__ = 'TIMETZ'

    def __init__(self, timezone=True, precision=None):
        # timezone param must be present as it's provided in base class so the
        # object can be instantiated with kwargs. see
        # :meth:`~sqlalchemy.dialects.postgresql.base.PGDialect._get_column_info`
        super(TIMETZ, self).__init__(timezone=True, precision=precision)


class GEOMETRY(RedshiftTypeEngine, sa.dialects.postgresql.TEXT):
    """
    Redshift defines a GEOMETRY column type
    https://docs.aws.amazon.com/redshift/latest/dg/c_Supported_data_types.html

    Adding an explicit type to the RedshiftDialect allows us follow the
    SqlAlchemy conventions for "vendor-specific types."

    https://docs.sqlalchemy.org/en/13/core/type_basics.html#vendor-specific-types
    """
    __visit_name__ = 'GEOMETRY'

    def __init__(self):
        super(GEOMETRY, self).__init__()

    def get_dbapi_type(self, dbapi):
        return dbapi.GEOMETRY


class SUPER(RedshiftTypeEngine, sa.dialects.postgresql.TEXT):
    """
    Redshift defines a SUPER column type
    https://docs.aws.amazon.com/redshift/latest/dg/c_Supported_data_types.html

    Adding an explicit type to the RedshiftDialect allows us follow the
    SqlAlchemy conventions for "vendor-specific types."

    https://docs.sqlalchemy.org/en/13/core/type_basics.html#vendor-specific-types
    """

    __visit_name__ = 'SUPER'

    def __init__(self):
        super(SUPER, self).__init__()

    def get_dbapi_type(self, dbapi):
        return dbapi.SUPER

    def bind_expression(self, bindvalue):
        return sa.func.json_parse(bindvalue)

    def process_bind_param(self, value, dialect):
        if not isinstance(value, str):
            return json.dumps(value)
        return value


class HLLSKETCH(RedshiftTypeEngine, sa.dialects.postgresql.TEXT):
    """
    Redshift defines a HLLSKETCH column type
    https://docs.aws.amazon.com/redshift/latest/dg/c_Supported_data_types.html

    Adding an explicit type to the RedshiftDialect allows us follow the
    SqlAlchemy conventions for "vendor-specific types."

    https://docs.sqlalchemy.org/en/13/core/type_basics.html#vendor-specific-types
    """
    __visit_name__ = 'HLLSKETCH'

    def __init__(self):
        super(HLLSKETCH, self).__init__()

    def get_dbapi_type(self, dbapi):
        return dbapi.HLLSKETCH


# Mapping for database schema inspection of Amazon Redshift datatypes
REDSHIFT_ISCHEMA_NAMES = {
    "geometry": GEOMETRY,
    "super": SUPER,
    "time with time zone": TIMETZ,
    "timestamp with time zone": TIMESTAMPTZ,
    "hllsketch": HLLSKETCH,
}


class RelationKey(namedtuple('RelationKey', ('name', 'schema'))):
    """
    Structured tuple of table/view name and schema name.
    """
    __slots__ = ()

    def __new__(cls, name, schema=None, connection=None):
        """
        Construct a new RelationKey with an explicit schema name.
        """
        if schema is None and connection is None:
            raise ValueError("Must specify either schema or connection")
        if schema is None:
            schema = inspect(connection).default_schema_name
        return super(RelationKey, cls).__new__(cls, name, schema)

    def __str__(self):
        if self.schema is None:
            return RelationKey._unquote(self.name)
        else:
            return RelationKey._unquote(self.schema) + "." + RelationKey._unquote(self.name)

    @staticmethod
    def _unquote(part):
        if (
                part is not None and part.startswith('"') and
                part.endswith('"')
        ):
            return part[1:-1]
        return part

    def unquoted(self):
        """
        Return *key* with one level of double quotes removed.

        Redshift stores some identifiers without quotes in internal tables,
        even though the name must be quoted elsewhere.
        In particular, this happens for tables named as a keyword.
        """
        return RelationKey(
            RelationKey._unquote(self.name),
            RelationKey._unquote(self.schema)
        )


class RedshiftCompiler(PGCompiler):

    def visit_array_agg_func(self, fn, **kw):
        return "LISTAGG(%s)" % self.function_argspec(fn, **kw)

    def visit_now_func(self, fn, **kw):
        return "SYSDATE"


class RedshiftDDLCompiler(PGDDLCompiler):
    """
    Handles Redshift-specific ``CREATE TABLE`` syntax.

    Users can specify the `diststyle`, `distkey`, `sortkey` and `encode`
    properties per table and per column.

    Table level properties can be set using the dialect specific syntax. For
    example, to specify a distribution key and style you apply the following:

    >>> import sqlalchemy as sa
    >>> from sqlalchemy.schema import CreateTable
    >>> engine = sa.create_engine('redshift+psycopg2://example')
    >>> metadata = sa.MetaData()
    >>> user = sa.Table(
    ...     'user',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String),
    ...     redshift_diststyle='KEY',
    ...     redshift_distkey='id',
    ...     redshift_interleaved_sortkey=['id', 'name'],
    ... )
    >>> print(CreateTable(user).compile(engine))
    <BLANKLINE>
    CREATE TABLE "user" (
        id INTEGER NOT NULL,
        name VARCHAR,
        PRIMARY KEY (id)
    ) DISTSTYLE KEY DISTKEY (id) INTERLEAVED SORTKEY (id, name)
    <BLANKLINE>
    <BLANKLINE>

    A single sort key can be applied without a wrapping list:

    >>> customer = sa.Table(
    ...     'customer',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String),
    ...     redshift_sortkey='id',
    ... )
    >>> print(CreateTable(customer).compile(engine))
    <BLANKLINE>
    CREATE TABLE customer (
        id INTEGER NOT NULL,
        name VARCHAR,
        PRIMARY KEY (id)
    ) SORTKEY (id)
    <BLANKLINE>
    <BLANKLINE>

    Column-level special syntax can also be applied using Redshift dialect
    specific keyword arguments.
    For example, we can specify the ENCODE for a column:

    >>> product = sa.Table(
    ...     'product',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String, redshift_encode='lzo')
    ... )
    >>> print(CreateTable(product).compile(engine))
    <BLANKLINE>
    CREATE TABLE product (
        id INTEGER NOT NULL,
        name VARCHAR ENCODE lzo,
        PRIMARY KEY (id)
    )
    <BLANKLINE>
    <BLANKLINE>

    The TIMESTAMPTZ and TIMETZ column types are also supported in the DDL.

    For SQLAlchemy versions < 1.3.0, passing Redshift dialect options
    as keyword arguments is not supported on the column level.
    Instead, a column info dictionary can be used:

    >>> product_pre_1_3_0 = sa.Table(
    ...     'product_pre_1_3_0',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String, info={'encode': 'lzo'})
    ... )

    We can also specify the distkey and sortkey options:

    >>> sku = sa.Table(
    ...     'sku',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column(
    ...         'name',
    ...         sa.String,
    ...         redshift_distkey=True,
    ...         redshift_sortkey=True
    ...     )
    ... )
    >>> print(CreateTable(sku).compile(engine))
    <BLANKLINE>
    CREATE TABLE sku (
        id INTEGER NOT NULL,
        name VARCHAR DISTKEY SORTKEY,
        PRIMARY KEY (id)
    )
    <BLANKLINE>
    <BLANKLINE>
    """

    def post_create_table(self, table):
        kwargs = ["diststyle", "distkey", "sortkey", "interleaved_sortkey"]
        info = table.dialect_options['redshift']
        info = {key: info.get(key) for key in kwargs}
        return get_table_attributes(self.preparer, **info)

    def get_column_specification(self, column, **kwargs):
        colspec = self.preparer.format_column(column)

        colspec += " " + self.dialect.type_compiler.process(column.type)

        default = self.get_column_default_string(column)
        if default is not None:
            # Identity constraints show up as *default* when reflected.
            m = IDENTITY_RE.match(default)
            if m:
                colspec += " IDENTITY({seed},{step})".format(**m.groupdict())
            else:
                colspec += " DEFAULT " + default

        colspec += self._fetch_redshift_column_attributes(column)

        if not column.nullable:
            colspec += " NOT NULL"
        return colspec

    def _fetch_redshift_column_attributes(self, column):
        text = ""
        if sa_version >= Version('1.3.0'):
            info = column.dialect_options['redshift']
        else:
            if not hasattr(column, 'info'):
                return text
            info = column.info

        identity = info.get('identity')
        if identity:
            text += " IDENTITY({0},{1})".format(identity[0], identity[1])

        encode = info.get('encode')
        if encode:
            text += " ENCODE " + encode

        distkey = info.get('distkey')
        if distkey:
            text += " DISTKEY"

        sortkey = info.get('sortkey')
        if sortkey:
            text += " SORTKEY"
        return text


class RedshiftTypeCompiler(PGTypeCompiler):

    def visit_GEOMETRY(self, type_, **kw):
        return "GEOMETRY"

    def visit_SUPER(self, type_, **kw):
        return "SUPER"

    def visit_TIMESTAMPTZ(self, type_, **kw):
        return "TIMESTAMPTZ"

    def visit_TIMETZ(self, type_, **kw):
        return "TIMETZ"

    def visit_HLLSKETCH(self, type_, **kw):
        return "HLLSKETCH"


class RedshiftIdentifierPreparer(PGIdentifierPreparer):
    reserved_words = RESERVED_WORDS

    def quote_schema(self, schema: Any, force: Optional[bool] = ...) -> str:
        return schema


class RedshiftDialectMixin(DefaultDialect):
    """
    Define Redshift-specific behavior.

    Most public methods are overrides of the underlying interfaces defined in
    :class:`~sqlalchemy.engine.interfaces.Dialect` and
    :class:`~sqlalchemy.engine.Inspector`.
    """

    name = 'redshift'
    max_identifier_length = 127

    statement_compiler = RedshiftCompiler
    ddl_compiler = RedshiftDDLCompiler
    preparer = RedshiftIdentifierPreparer
    identifier_preparer = RedshiftIdentifierPreparer
    type_compiler = RedshiftTypeCompiler
    construct_arguments = [
        (sa.schema.Index, {
            "using": False,
            "where": None,
            "ops": {}
        }),
        (sa.schema.Table, {
            "ignore_search_path": False,
            "datashare":None,
            "diststyle": None,
            "distkey": None,
            "sortkey": None,
            "interleaved_sortkey": None,
        }),
        (sa.schema.Column, {
            "datashare":None,
            "encode": None,
            "distkey": None,
            "sortkey": None,
            "identity": None,
        }),
    ]

    def __init__(self, *args, **kw):
        super(RedshiftDialectMixin, self).__init__(*args, **kw)
        # Cache domains, as these will be static;
        # Redshift does not support user-created domains.
        self._domains = None

    @property
    def ischema_names(self):
        """
        Returns information about datatypes supported by Amazon Redshift.

        Used in
        :meth:`~sqlalchemy.engine.dialects.postgresql.base.PGDialect._get_column_info`.
        """
        return {
            **super(RedshiftDialectMixin, self).ischema_names,
            **REDSHIFT_ISCHEMA_NAMES
        }
    def get_multi_indexes(self, connection, **kw):
        return self._default_multi_reflect(self.get_indexes, connection, **kw)

    def get_multi_foreign_keys(self, connection, **kw):
        return self._default_multi_reflect(self.get_foreign_keys, connection, **kw)

    def get_multi_pk_constraint(self, connection, **kw):
        return self._default_multi_reflect(self.get_pk_constraint, connection, **kw)

    def get_multi_unique_constraints(self, connection, **kw):
        return self._default_multi_reflect(self.get_unique_constraints, connection, **kw)

    def get_multi_columns(self, connection, **kw):
        return self._default_multi_reflect(self.get_columns, connection, **kw)

    def get_temp_table_names(self, *args, **kwargs) -> List[str]:
        return []

    # Copied from SQLAlchemy 1.4.0
    # https://github.com/sqlalchemy/sqlalchemy/blob/rel_1_4_54/lib/sqlalchemy/dialects/postgresql/base.py#L4741-L4778
    def _load_domains(self, connection):
        # Load data types for domains:
        SQL_DOMAINS = """
            SELECT t.typname as "name",
               pg_catalog.format_type(t.typbasetype, t.typtypmod) as "attype",
               not t.typnotnull as "nullable",
               t.typdefault as "default",
               pg_catalog.pg_type_is_visible(t.oid) as "visible",
               n.nspname as "schema"
            FROM pg_catalog.pg_type t
               LEFT JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typtype = 'd'
        """

        s = sa.text(SQL_DOMAINS)
        c = connection.execution_options(future_result=True).execute(s)

        domains = {}
        for domain in c.mappings():
            domain = domain
            # strip (30) from character varying(30)
            attype = re.search(r"([^\(]+)", domain["attype"]).group(1)
            # 'visible' just means whether or not the domain is in a
            # schema that's on the search path -- or not overridden by
            # a schema with higher precedence. If it's not visible,
            # it will be prefixed with the schema-name when it's used.
            if domain["visible"]:
                key = (domain["name"],)
            else:
                key = (domain["schema"], domain["name"])

            domains[key] = {
                "attype": attype,
                "nullable": domain["nullable"],
                "default": domain["default"],
            }

        return domains

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        """
        Return information about columns in `table_name`.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_columns`.
        """
        cols = self._get_redshift_columns(connection, table_name, schema, **kw)
        if not self._domains:
            self._domains = self._load_domains(connection)
        domains = self._domains
        columns = []
        for col in cols:
            column_info = self._get_redshift_column_info(
                name=col.name, format_type=col.format_type,
                default=col.default, notnull=col.notnull, domains=domains,
                enums=[], schema=col.schema, encode=col.encode,
                comment=col.comment)
            columns.append(column_info)
        return columns

    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kw):
        if not schema:
            schema = inspect(connection).default_schema_name

        info_cache = kw.get('info_cache')
        table = self._get_all_relation_info(connection,
                                            schema=schema,
                                            table_name=table_name,
                                            info_cache=info_cache)

        return True if table else False

    @reflection.cache
    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        datashare, _, has_datashare = parse_datashare(schema)
        table_oid = self.get_table_oid(
            connection, table_name, schema, info_cache=kw.get("info_cache"), datashare=datashare,
        )
        table_oid = 'NULL' if not table_oid else table_oid

        result = connection.execute(sa.text("""
                        SELECT
                            cons.conname as name,
                            pg_get_constraintdef(cons.oid) as src
                        FROM
                            pg_catalog.pg_constraint cons
                        WHERE
                            cons.conrelid = {} AND
                            cons.contype = 'c'
                        """.format(table_oid)))
        ret = []
        for name, src in result:
            # samples:
            # "CHECK (((a > 1) AND (a < 5)))"
            # "CHECK (((a = 1) OR ((a > 2) AND (a < 5))))"
            # "CHECK (((a > 1) AND (a < 5))) NOT VALID"
            # "CHECK (some_boolean_function(a))"
            # "CHECK (((a\n < 1)\n OR\n (a\n >= 5))\n)"

            m = re.match(
                r"^CHECK *\((.+)\)( NOT VALID)?$", src, flags=re.DOTALL
            )
            if not m:
                logger.warning(f"Could not parse CHECK constraint text: {src}")
                sqltext = ""
            else:
                sqltext = re.compile(
                    r"^[\s\n]*\((.+)\)[\s\n]*$", flags=re.DOTALL
                ).sub(r"\1", m.group(1))
            entry = {"name": name, "sqltext": sqltext}
            if m and m.group(2):
                entry["dialect_options"] = {"not_valid": True}

            ret.append(entry)
        return ret

    @reflection.cache
    def get_table_oid(self, connection, table_name, schema=None, **kw):
        """Fetch the oid for schema.table_name.
        Return None if not found (external table does not have table oid)"""

        datashare, clean_schema, has_datashare = parse_datashare(schema)
        schema_field = '{schema}.'.format(schema=schema) if schema else ""

        try:
            result = connection.execute(
                sa.text(
                    """
                    select '{schema_field}{table_name}'::regclass::oid;
                    """.format(
                        schema_field=schema_field,
                        table_name=table_name
                    )
                )
            )

            return result.scalar()
        except Exception as e:
            # Gracefully handle external tables
            schema_filter = (
                "AND schemaname = '{schema}'".format(schema=schema)
                if schema else ""
            )
            if not has_datashare:
                result = connection.execute(
                    sa.text(
                        """
                        SELECT
                            1
                        FROM svv_external_tables
                        WHERE
                            tablename = '{table_name}'
                            {schema_filter}
                        LIMIT 1;
                        """.format(
                            schema_filter=schema_filter,
                            table_name=table_name
                        )
                    )
                )
            else:
                datashare_filter = "AND database_name = '{datashare}'".format(datashare=datashare)
                schema_filter = (
                    "AND schema_name = '{schema}'".format(schema=clean_schema)
                    if clean_schema else ""
                )
                result = connection.execute(
                    sa.text(
                        """
                        SELECT
                            1
                        FROM svv_redshift_tables
                        WHERE
                            table_name = '{table_name}'
                            {schema_filter}
                            {datashare_filter}
                        LIMIT 1;
                        """.format(
                            schema_filter=schema_filter,
                            datashare_filter=datashare_filter,
                            table_name=table_name
                        )
                    )
                )
            if result.scalar() is not None:
                return None
            raise e

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        """
        Return information about the primary key constraint on `table_name`.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_pk_constraint`.
        """
        constraints = self._get_redshift_constraints(connection, table_name,
                                                     schema, **kw)
        pk_constraints = [c for c in constraints if c.contype == 'p']
        if not pk_constraints:
            return {'constrained_columns': [], 'name': ''}
        pk_constraint = pk_constraints[0]
        m = PRIMARY_KEY_RE.match(pk_constraint.condef)
        colstring = m.group('columns')
        constrained_columns = SQL_IDENTIFIER_RE.findall(colstring)
        return {
            'constrained_columns': constrained_columns,
            'name': pk_constraint.conname,
        }

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        """
        Return information about foreign keys in `table_name`.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_pk_constraint`.
        """
        constraints = self._get_redshift_constraints(connection, table_name,
                                                     schema, **kw)
        fk_constraints = [c for c in constraints if c.contype == 'f']
        uniques = defaultdict(lambda: defaultdict(dict))
        for con in fk_constraints:
            uniques[con.conname]["key"] = con.conkey
            uniques[con.conname]["condef"] = con.condef
        fkeys = []
        for conname, attrs in uniques.items():
            m = FOREIGN_KEY_RE.match(attrs['condef'])
            colstring = m.group('referred_columns')
            referred_columns = SQL_IDENTIFIER_RE.findall(colstring)
            referred_table = m.group('referred_table')
            referred_schema = m.group('referred_schema')
            colstring = m.group('columns')
            constrained_columns = SQL_IDENTIFIER_RE.findall(colstring)
            fkey_d = {
                'name': conname,
                'constrained_columns': constrained_columns,
                'referred_schema': self.unquote(referred_schema),
                'referred_table': self.unquote(referred_table),
                'referred_columns': referred_columns,
            }
            fkeys.append(fkey_d)
        return fkeys

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        """
        Return a list of table names for `schema`.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_table_names`.
        """
        return self._get_table_or_view_names('r', connection, schema, **kw)

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        """
        Return a list of view names for `schema`.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_view_names`.
        """
        return self._get_table_or_view_names('v', connection, schema, **kw)

    @reflection.cache
    def get_view_definition(self, connection, view_name, schema=None, **kw):
        """Return view definition.
        Given a :class:`.Connection`, a string `view_name`,
        and an optional string `schema`, return the view definition.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_view_definition`.
        """
        view = self._get_redshift_relation(connection, view_name, schema, **kw)
        return sa.text(view.view_definition)

    def get_indexes(self, connection, table_name, schema, **kw):
        """
        Return information about indexes in `table_name`.

        Because Redshift does not support traditional indexes,
        this always returns an empty list.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_indexes`.
        """
        return []

    @staticmethod
    def unquote(text):
        if text is None:
            return None

        if text.startswith('"') and text.endswith('"'):
            return text[1:-1]

        return text

    @reflection.cache
    def get_unique_constraints(self, connection, table_name,
                               schema=None, **kw):
        """
        Return information about unique constraints in `table_name`.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.get_unique_constraints`.
        """
        constraints = self._get_redshift_constraints(connection,
                                                     table_name, schema, **kw)
        constraints = [c for c in constraints if c.contype == 'u']
        uniques = defaultdict(lambda: defaultdict(dict))
        for con in constraints:
            uniques[con.conname]["key"] = con.conkey
            uniques[con.conname]["cols"][con.attnum] = con.attname

        return [
            {'name': name,
             'column_names': [uc["cols"][i] for i in uc["key"]]}
            for name, uc in uniques.items()
        ]

    @reflection.cache
    def get_table_options(self, connection, table_name, schema, **kw):
        """
        Return a dictionary of options specified when the table of the
        given name was created.

        Overrides interface
        :meth:`~sqlalchemy.engine.Inspector.get_table_options`.
        """
        def keyfunc(column):
            num = int(column.sortkey)
            # If sortkey is interleaved, column numbers alternate
            # negative values, so take abs.
            return abs(num)
        table = self._get_redshift_relation(connection, table_name,
                                            schema, **kw)
        columns = self._get_redshift_columns(connection, table_name,
                                             schema, **kw)
        sortkey_cols = sorted([col for col in columns if col.sortkey],
                              key=keyfunc)
        interleaved = any([int(col.sortkey) < 0 for col in sortkey_cols])
        sortkey = tuple(col.name for col in sortkey_cols)
        interleaved_sortkey = None
        if interleaved:
            interleaved_sortkey = sortkey
            sortkey = None
        distkeys = [col.name for col in columns if col.distkey]
        distkey = distkeys[0] if distkeys else None
        return {
            'redshift_datashare': None,
            'redshift_diststyle': table.diststyle,
            'redshift_distkey': distkey,
            'redshift_sortkey': sortkey,
            'redshift_interleaved_sortkey': interleaved_sortkey,
        }

    def _get_table_or_view_names(self, relkind, connection, schema=None, **kw):
        default_schema = inspect(connection).default_schema_name
        if not schema:
            schema = default_schema
        info_cache = kw.get('info_cache')
        all_relations = self._get_all_relation_info(connection,
                                                    schema=schema,
                                                    info_cache=info_cache)
        relation_names = []
        for key, relation in all_relations.items():
            if key.schema == schema and relation.relkind == relkind:
                relation_names.append(self.unquote(key.name))
        return relation_names


    # Copied from SQLAlchemy 1.4
    # https://github.com/sqlalchemy/sqlalchemy/blob/rel_1_4_54/lib/sqlalchemy/dialects/postgresql/base.py#L4008C4-L4189C27
    def _get_column_info(
        self,
        name,
        format_type,
        default,
        notnull,
        domains,
        enums,
        schema,
        comment,
        generated,
        identity,
    ):
        def _handle_array_type(attype):
            return (
                # strip '[]' from integer[], etc.
                re.sub(r"\[\]$", "", attype),
                attype.endswith("[]"),
            )

        if format_type is None:
            no_format_type = True
            attype = format_type = "no format_type()"
            is_array = False
        else:
            no_format_type = False

            # strip (*) from character varying(5), timestamp(5)
            # with time zone, geometry(POLYGON), etc.
            attype = re.sub(r"\(.*\)", "", format_type)

            # strip '[]' from integer[], etc. and check if an array
            attype, is_array = _handle_array_type(attype)

        # strip quotes from case sensitive enum or domain names
        enum_or_domain_key = tuple(util.quoted_token_parser(attype))

        nullable = not notnull

        charlen = re.search(r"\(([\d,]+)\)", format_type)
        if charlen:
            charlen = charlen.group(1)
        args = re.search(r"\((.*)\)", format_type)
        if args and args.group(1):
            args = tuple(re.split(r"\s*,\s*", args.group(1)))
        else:
            args = ()
        kwargs = {}

        if attype == "numeric":
            if charlen:
                prec, scale = charlen.split(",")
                args = (int(prec), int(scale))
            else:
                args = ()
        elif attype == "double precision":
            args = (53,)
        elif attype == "integer":
            args = ()
        elif attype in ("timestamp with time zone", "time with time zone"):
            kwargs["timezone"] = True
            if charlen:
                kwargs["precision"] = int(charlen)
            args = ()
        elif attype in (
            "timestamp without time zone",
            "time without time zone",
            "time",
        ):
            kwargs["timezone"] = False
            if charlen:
                kwargs["precision"] = int(charlen)
            args = ()
        elif attype == "bit varying":
            kwargs["varying"] = True
            if charlen:
                args = (int(charlen),)
            else:
                args = ()
        elif attype.startswith("interval"):
            field_match = re.match(r"interval (.+)", attype, re.I)
            if charlen:
                kwargs["precision"] = int(charlen)
            if field_match:
                kwargs["fields"] = field_match.group(1)
            attype = "interval"
            args = ()
        elif charlen:
            args = (int(charlen),)

        while True:
            # looping here to suit nested domains
            if attype in self.ischema_names:
                coltype = self.ischema_names[attype]
                break
            elif enum_or_domain_key in enums:
                enum = enums[enum_or_domain_key]
                coltype = ENUM
                kwargs["name"] = enum["name"]
                if not enum["visible"]:
                    kwargs["schema"] = enum["schema"]
                args = tuple(enum["labels"])
                break
            elif enum_or_domain_key in domains:
                domain = domains[enum_or_domain_key]
                attype = domain["attype"]
                attype, is_array = _handle_array_type(attype)
                # strip quotes from case sensitive enum or domain names
                enum_or_domain_key = tuple(util.quoted_token_parser(attype))
                # A table can't override a not null on the domain,
                # but can override nullable
                nullable = nullable and domain["nullable"]
                if domain["default"] and not default:
                    # It can, however, override the default
                    # value, but can't set it to null.
                    default = domain["default"]
                continue
            else:
                coltype = None
                break

        if coltype:
            coltype = coltype(*args, **kwargs)
            if is_array:
                coltype = self.ischema_names["_array"](coltype)
        elif no_format_type:
            util.warn(
                "PostgreSQL format_type() returned NULL for column '%s'"
                % (name,)
            )
            coltype = sqltypes.NULLTYPE
        else:
            util.warn(
                "Did not recognize type '%s' of column '%s'" % (attype, name)
            )
            coltype = sqltypes.NULLTYPE

        # If a zero byte or blank string depending on driver (is also absent
        # for older PG versions), then not a generated column. Otherwise, s =
        # stored. (Other values might be added in the future.)
        if generated not in (None, "", b"\x00"):
            computed = dict(
                sqltext=default, persisted=generated in ("s", b"s")
            )
            default = None
        else:
            computed = None

        # adjust the default value
        autoincrement = False
        if default is not None:
            match = re.search(r"""(nextval\(')([^']+)('.*$)""", default)
            if match is not None:
                if issubclass(coltype._type_affinity, sqltypes.Integer):
                    autoincrement = True
                # the default is related to a Sequence
                sch = schema
                if "." not in match.group(2) and sch is not None:
                    # unconditionally quote the schema name.  this could
                    # later be enhanced to obey quoting rules /
                    # "quote schema"
                    default = (
                        match.group(1)
                        + ('"%s"' % sch)
                        + "."
                        + match.group(2)
                        + match.group(3)
                    )

        column_info = dict(
            name=name,
            type=coltype,
            nullable=nullable,
            default=default,
            autoincrement=autoincrement or identity is not None,
            comment=comment,
        )
        if computed is not None:
            column_info["computed"] = computed
        if identity is not None:
            column_info["identity"] = identity
        return column_info

    def _get_redshift_column_info(self, *args, **kwargs):
        kw = kwargs.copy()
        encode = kw.pop('encode', None)
        if sa_version >= Version('1.3.16'):
            # SQLAlchemy 1.3.16 introduced generated columns,
            # not supported in redshift
            kw['generated'] = ''

        if sa_version < Version('1.4.0') and 'identity' in kw:
            del kw['identity']
        elif sa_version >= Version('1.4.0') and 'identity' not in kw:
            kw['identity'] = None

        column_info = self._get_column_info(
            *args,
            **kw
        )
        if isinstance(column_info['type'], VARCHAR):
            if column_info['type'].length is None:
                column_info['type'] = NullType()
        if 'info' not in column_info:
            column_info['info'] = {}
        if encode and encode != 'none':
            column_info['info']['encode'] = encode
        return column_info

    def _get_redshift_relation(self, connection, table_name,
                               schema=None, **kw):
        info_cache = kw.get('info_cache')
        all_relations = self._get_all_relation_info(connection,
                                                    schema=schema,
                                                    table_name=table_name,
                                                    info_cache=info_cache)
        key = RelationKey(table_name, schema, connection)
        if key not in all_relations.keys():
            key = key.unquoted()
        try:
            return all_relations[key]
        except KeyError:
            raise sa.exc.NoSuchTableError(key)

    def _get_redshift_columns(self, connection, table_name, schema=None, **kw):
        info_cache = kw.get('info_cache')
        all_schema_columns = self._get_schema_column_info(
            connection,
            schema=schema,
            table_name=table_name,
            info_cache=info_cache
        )
        key = RelationKey(table_name, schema, connection)
        if key not in all_schema_columns.keys():
            key = key.unquoted()
        return all_schema_columns[key]

    def _get_redshift_constraints(self, connection, table_name,
                                  schema=None, **kw):
        info_cache = kw.get('info_cache')
        all_constraints = self._get_all_constraint_info(connection,
                                                        schema=schema,
                                                        table_name=table_name,
                                                        info_cache=info_cache)
        key = RelationKey(table_name, schema, connection)
        if key not in all_constraints.keys():
            key = key.unquoted()
        return all_constraints[key]

    @reflection.cache
    def _get_all_relation_info(self, connection, **kw):
        schema = kw.get('schema', None)
        # This goes before using schema because it does cleanup
        datashare, _, has_datashare = parse_datashare(schema)
        datashare_clause = (
                "AND s.database_name = '{datashare}'".format(datashare=datashare) if has_datashare else ""
        )
        schema_clause = (
            "AND schema = '{schema}'".format(schema=schema) if schema else ""
        )

        ## This could also be passed but, if not then present in the schema it needs to be added
        ## because subsequent reflective calls do not pass through custom attributes.
        # datashare = kw.get('datashare', None)




        table_name = kw.get('table_name', None)
        table_clause = (
            "AND relname = '{table}'".format(
                table=table_name
            ) if table_name else ""
        )

        result = connection.execute(sa.text("""
        SELECT
          c.relkind,
          n.oid as "schema_oid",
          n.nspname as "schema",
          c.oid as "rel_oid",
          c.relname,
          CASE c.reldiststyle
            WHEN 0 THEN 'EVEN' WHEN 1 THEN 'KEY' WHEN 8 THEN 'ALL' END
            AS "diststyle",
          c.relowner AS "owner_id",
          u.usename AS "owner_name",
          TRIM(TRAILING ';' FROM pg_catalog.pg_get_viewdef(c.oid, true))
            AS "view_definition",
          pg_catalog.array_to_string(c.relacl, '\n') AS "privileges"
        FROM pg_catalog.pg_class c
             LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             JOIN pg_catalog.pg_user u ON u.usesysid = c.relowner
        WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
          AND n.nspname !~ '^pg_' {schema_clause} {table_clause}
        UNION
        SELECT
            'r' AS "relkind",
            s.esoid AS "schema_oid",
            s.schemaname AS "schema",
            null AS "rel_oid",
            t.tablename AS "relname",
            null AS "diststyle",
            s.esowner AS "owner_id",
            u.usename AS "owner_name",
            null AS "view_definition",
            null AS "privileges"
        FROM
            svv_external_tables t
            JOIN svv_external_schemas s ON s.schemaname = t.schemaname
            JOIN pg_catalog.pg_user u ON u.usesysid = s.esowner
        where 1 {schema_clause} {table_clause}
        UNION
        SELECT
            'r' AS relkind,
            NULL AS schema_oid,  -- Not available in svv_redshift_schemas
            s.database_name || '.' || s.schema_name AS schema,
            NULL AS rel_oid,
            t.table_name AS relname,
            NULL AS diststyle,
            NULL AS owner_id,
            NULL AS owner_name,
            NULL AS view_definition,
            NULL AS privileges
        FROM
            svv_redshift_tables t
            LEFT JOIN svv_redshift_schemas s ON s.schema_name = t.schema_name
        WHERE 1
            {datashare_clause} {schema_clause} {table_clause}
        ORDER BY "relkind", "schema_oid", "schema";

       
        """.format(schema_clause=schema_clause, table_clause=table_clause,datashare_clause=datashare_clause)))
        relations = {}
        for rel in result:
            key = RelationKey(rel.relname, rel.schema, connection)
            relations[key] = rel
        return relations

    # We fetch column info an entire schema at a time to improve performance
    # when reflecting schema for multiple tables at once.
    @reflection.cache
    def _get_schema_column_info(self, connection, **kw):
        schema = kw.get('schema', None)
        schema = self.unquote(schema)
        # datashare = kw.get('datashare', None)
        datashare, _, has_datashare = parse_datashare(schema)
        schema_clause = (
            "AND schema = '{schema}'".format(schema=schema) if schema else ""
        )


        datashare_clause = (
                "AND c.database_name = '{datashare}'".format(datashare=datashare) if datashare else ""
        )

        table_name = kw.get('table_name', None)
        table_clause = (
            "AND table_name = '{table}'".format(
                table=table_name
            ) if table_name else ""
        )

        unambiguous_tablename_clause = (
            "AND c.table_name = '{table}'".format(
                table=table_name
            ) if table_name else ""
        )

        all_columns = defaultdict(list)
        result = connection.execute(sa.text(REFLECTION_SQL.format(
            schema_clause=schema_clause,
            table_clause=table_clause,
            unambiguous_tablename_clause=unambiguous_tablename_clause,
            datashare_clause=datashare_clause,
        )))

        for col in result:
            key = RelationKey(col.table_name, col.schema, connection)
            all_columns[key].append(col)

        return dict(all_columns)

    @reflection.cache
    def _get_all_constraint_info(self, connection, **kw):
        schema = kw.get('schema', None)
        #datashare = kw.get('datashare', None)
        datashare, _, has_datashare = parse_datashare(schema)
        schema_clause = (
            "AND schema = '{schema}'".format(schema=schema) if schema else ""
        )


        datashare_clause = (
                "AND c.database_name = '{datashare}'".format(datashare=datashare) if datashare else ""
        )

        table_name = kw.get('table_name', None)
        table_clause = (
            "AND table_name = '{table}'".format(
                table=table_name
            ) if table_name else ""
        )
        unambiguous_table_clause = (
            "AND c.table_name = '{table}'".format(
                table=table_name
            ) if table_name else ""
        )

        result = connection.execute(sa.text("""
        SELECT
          n.nspname as "schema",
          c.relname as "table_name",
          t.contype,
          t.conname,
          t.conkey,
          a.attnum,
          a.attname,
          pg_catalog.pg_get_constraintdef(t.oid, true)::varchar(512) as condef,
          n.oid as "schema_oid",
          c.oid as "rel_oid"
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n
          ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_constraint t
          ON t.conrelid = c.oid
        JOIN pg_catalog.pg_attribute a
          ON t.conrelid = a.attrelid AND a.attnum = ANY(t.conkey)
        WHERE n.nspname !~ '^pg_' {schema_clause} {table_clause}
        UNION
        SELECT
            s.schemaname AS "schema",
            c.tablename AS "table_name",
            'p' as "contype",
            c.tablename || '_pkey' as "conname",
            array[1::SMALLINT] as "conkey",
            1 as "attnum",
            c.columnname as "attname",
            'PRIMARY KEY (' || c.columnname  || ')'::VARCHAR(512) as "condef",
            s.esoid AS "schema_oid",
            null AS "rel_oid"
        FROM
            svv_external_columns c
            JOIN svv_external_schemas s ON s.schemaname = c.schemaname
        where 1 {schema_clause} {table_clause}
        UNION
        SELECT
            c.database_name || '.' || c.schema_name AS "schema",
            c.table_name AS "table_name",
            'p' AS "contype",
            c.table_name || '_pkey' AS "conname",
            array[1::SMALLINT] AS "conkey",
            1 AS "attnum",
            c.column_name AS "attname",
            'PRIMARY KEY (' || c.column_name || ')'::VARCHAR(512) AS "condef",
            null AS "schema_oid",
            null AS "rel_oid"
        FROM
            svv_redshift_columns c
            JOIN svv_redshift_tables t ON t.schema_name = c.schema_name AND t.table_name = c.table_name
        WHERE 1 {datashare_clause} {schema_clause} {unambiguous_table_clause}
        ORDER BY "schema", "table_name"
        """.format(schema_clause=schema_clause, table_clause=table_clause,
                   unambiguous_table_clause=unambiguous_table_clause, datashare_clause=datashare_clause)))
        all_constraints = defaultdict(list)
        for con in result:
            key = RelationKey(con.table_name, con.schema, connection)
            all_constraints[key].append(con)
        return all_constraints

    def _set_backslash_escapes(self, connection):
        self._backslash_escapes = False


class Psycopg2RedshiftDialectMixin(RedshiftDialectMixin):
    """
    Define behavior specific to ``psycopg2``.

    Most public methods are overrides of the underlying interfaces defined in
    :class:`~sqlalchemy.engine.interfaces.Dialect` and
    :class:`~sqlalchemy.engine.Inspector`.
    """
    def create_connect_args(self, *args, **kwargs):
        """
        Build DB-API compatible connection arguments.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.create_connect_args`.
        """
        cwd = Path(__file__).parent.resolve()
        default_args = {
            'sslmode': 'verify-full',
            'sslrootcert': str(cwd / 'redshift-ca-bundle.crt'),
        }
        cargs, cparams = (
            super(Psycopg2RedshiftDialectMixin, self).create_connect_args(
                *args, **kwargs
            )
        )
        default_args.update(cparams)
        return cargs, default_args

    @classmethod
    def dbapi(cls):
        try:
            return importlib.import_module(cls.driver)
        except ImportError:
            raise ImportError(
                'No module named {}'.format(cls.driver)
            )


class RedshiftDialect_psycopg2(
    Psycopg2RedshiftDialectMixin, PGDialect_psycopg2
):
    supports_statement_cache = False


# Add RedshiftDialect synonym for backwards compatibility.
RedshiftDialect = RedshiftDialect_psycopg2


class RedshiftDialect_psycopg2cffi(
    Psycopg2RedshiftDialectMixin, PGDialect_psycopg2cffi
):
    supports_statement_cache = False


class RedshiftDialect_redshift_connector(RedshiftDialectMixin, PGDialect):

    class RedshiftCompiler_redshift_connector(RedshiftCompiler, PGCompiler):
        def limit_clause(self, select, **kw):
            text = ""
            if select._limit_clause is not None:
                # an integer value for limit is retrieved
                text += " \n LIMIT " + str(select._limit)
            if select._offset_clause is not None:
                if select._limit_clause is None:
                    text += "\n LIMIT ALL"
                # an integer value for offset is retrieved
                text += " OFFSET " + str(select._offset)
            return text

        def visit_mod_binary(self, binary, operator, **kw):
            return (
                self.process(binary.left, **kw)
                + " %% "
                + self.process(binary.right, **kw)
            )

        def post_process_text(self, text):
            from sqlalchemy import util
            if "%%" in text:
                util.warn(
                    "The SQLAlchemy postgresql dialect "
                    "now automatically escapes '%' in text() "
                    "expressions to '%%'."
                )
            return text.replace("%", "%%")

    class RedshiftExecutionContext_redshift_connector(PGExecutionContext):
        def pre_exec(self):
            if not self.compiled:
                return

    driver = 'redshift_connector'

    supports_unicode_statements = True

    supports_unicode_binds = True

    default_paramstyle = "format"
    supports_sane_multi_rowcount = True
    statement_compiler = RedshiftCompiler_redshift_connector
    execution_ctx_cls = RedshiftExecutionContext_redshift_connector

    supports_statement_cache = False
    use_setinputsizes = False  # not implemented in redshift_connector

    def __init__(self, client_encoding=None, **kwargs):
        super(
            RedshiftDialect_redshift_connector, self
        ).__init__(client_encoding=client_encoding, **kwargs)
        self.client_encoding = client_encoding

    @classmethod
    def dbapi(cls):
        try:
            driver_module = importlib.import_module(cls.driver)

            # Starting v2.0.908 driver converts description column names to str
            if Version(driver_module.__version__) < Version('2.0.908'):
                cls.description_encoding = "use_encoding"
            else:
                cls.description_encoding = None

            return driver_module
        except ImportError:
            raise ImportError(
                'No module named redshift_connector. Please install '
                'redshift_connector to use this sqlalchemy dialect.'
            )

    def set_client_encoding(self, connection, client_encoding):
        """
        Sets the client-side encoding using the provided connection object.
        """
        # adjust for ConnectionFairy possibly being present
        if hasattr(connection, "connection"):
            connection = connection.connection

        cursor = connection.cursor()
        cursor.execute("SET CLIENT_ENCODING TO '" + client_encoding + "'")
        cursor.execute("COMMIT")
        cursor.close()

    def set_isolation_level(self, connection, level):
        """
        Sets the isolation level for the current transaction.

        Additionally, autocommit can be enabled on the underlying
        db-api connection object via argument level='AUTOCOMMIT'.

        See Amazon Redshift documentation for information on supported
        isolation levels.
        https://docs.aws.amazon.com/redshift/latest/dg/r_BEGIN.html
        """
        level = level.replace("_", " ")

        # adjust for ConnectionFairy possibly being present
        if hasattr(connection, "connection"):
            connection = connection.connection

        if level == "AUTOCOMMIT":
            connection.autocommit = True
        else:
            connection.autocommit = False
            super(
                RedshiftDialect_redshift_connector, self
            ).set_isolation_level(connection, level)

    def on_connect(self):
        fns = []

        def on_connect(conn):
            from sqlalchemy.sql.elements import quoted_name
            conn.py_types[quoted_name] = conn.py_types[str]

        fns.append(on_connect)

        if self.client_encoding is not None:

            def on_connect(conn):
                self.set_client_encoding(conn, self.client_encoding)

            fns.append(on_connect)

        if self.isolation_level is not None:

            def on_connect(conn):
                self.set_isolation_level(conn, self.isolation_level)

            fns.append(on_connect)

        if len(fns) > 0:

            def on_connect(conn):
                for fn in fns:
                    fn(conn)

            return on_connect
        else:
            return None

    def create_connect_args(self, *args, **kwargs):
        """
        Build DB-API compatible connection arguments.

        Overrides interface
        :meth:`~sqlalchemy.engine.interfaces.Dialect.create_connect_args`.
        """
        default_args = {
            'sslmode': 'verify-full',
            'ssl': True,
            'application_name': 'sqlalchemy-redshift'
        }
        cargs, cparams = super(RedshiftDialectMixin, self).create_connect_args(
            *args, **kwargs
        )
        # set client_encoding so it is picked up by on_connect(), as
        # redshift_connector does not have client_encoding connection parameter
        self.client_encoding = cparams.pop(
            'client_encoding', self.client_encoding
        )

        if 'port' in cparams:
            cparams['port'] = int(cparams['port'])

        if 'username' in cparams:
            cparams['user'] = cparams['username']
            del cparams['username']

        default_args.update(cparams)
        return cargs, default_args


def gen_columns_from_children(root):
    """
    Generates columns that are being used in child elements of the delete query
    this will be used to determine tables for the using clause.
    :param root: the delete query
    :return: a generator of columns
    """
    if isinstance(root, (Delete, BinaryExpression, BooleanClauseList)):
        for child in root.get_children():
            yc = gen_columns_from_children(child)
            for it in yc:
                yield it
    elif isinstance(root, sa.Column):
        yield root


@compiles(Delete, 'redshift')
def visit_delete_stmt(element, compiler, **kwargs):
    """
    Adds redshift-dialect specific compilation rule for the
    delete statement.

    Redshift DELETE syntax can be found here:
    https://docs.aws.amazon.com/redshift/latest/dg/r_DELETE.html

    .. :code-block: sql

        DELETE [ FROM ] table_name
        [ { USING } table_name, ...]
        [ WHERE condition ]

    By default, SqlAlchemy compiles DELETE statements with the
    syntax:

    .. :code-block: sql

        DELETE [ FROM ] table_name
        [ WHERE condition ]

    problem illustration:

    >>> from sqlalchemy import Table, Column, Integer, MetaData, delete
    >>> from sqlalchemy_redshift.dialect import RedshiftDialect_psycopg2
    >>> meta = MetaData()
    >>> table1 = Table(
    ... 'table_1',
    ... meta,
    ... Column('pk', Integer, primary_key=True)
    ... )
    ...
    >>> table2 = Table(
    ... 'table_2',
    ... meta,
    ... Column('pk', Integer, primary_key=True)
    ... )
    ...
    >>> del_stmt = delete(table1).where(table1.c.pk==table2.c.pk)
    >>> str(del_stmt.compile(dialect=RedshiftDialect_psycopg2()))
    'DELETE FROM table_1 USING table_2 WHERE table_1.pk = table_2.pk'
    >>> str(del_stmt)
    'DELETE FROM table_1 , table_2 WHERE table_1.pk = table_2.pk'
    >>> del_stmt2 = delete(table1)
    >>> str(del_stmt2)
    'DELETE FROM table_1'
    >>> del_stmt3 = delete(table1).where(table1.c.pk > 1000)
    >>> str(del_stmt3)
    'DELETE FROM table_1 WHERE table_1.pk > :pk_1'
    >>> str(del_stmt3.compile(dialect=RedshiftDialect_psycopg2()))
    'DELETE FROM table_1 WHERE table_1.pk >  %(pk_1)s'
    """

    # Set empty strings for the default where clause and using clause
    whereclause = ''
    usingclause = ''

    # determine if the delete query needs a ``USING`` injected
    # by inspecting the whereclause's children & their children...
    # first, the where clause text is buit, if applicable
    # then, the using clause text is built, if applicable
    # note:
    #   the tables in the using clause are sorted in the order in
    #   which they first appear in the where clause.
    delete_stmt_table = compiler.process(element.table, asfrom=True, **kwargs)

    if sa_version >= Version('1.4.0'):
        if element.whereclause is not None:
            clause = compiler.process(element.whereclause, **kwargs)
            if clause:
                whereclause = ' WHERE {clause}'.format(clause=clause)
    else:
        whereclause_tuple = element.get_children()
        if whereclause_tuple:
            whereclause = ' WHERE {clause}'.format(
                clause=compiler.process(*whereclause_tuple, **kwargs)
            )

    if whereclause:
        usingclause_tables = []
        whereclause_columns = gen_columns_from_children(element)
        for col in whereclause_columns:
            table = compiler.process(col.table, asfrom=True, **kwargs)
            if table != delete_stmt_table and \
                    table not in usingclause_tables:
                usingclause_tables.append(table)
        if usingclause_tables:
            usingclause = ' USING {clause}'.format(
                clause=', '.join(usingclause_tables)
            )

    return 'DELETE FROM {table}{using}{where}'.format(
        table=delete_stmt_table,
        using=usingclause,
        where=whereclause)
