# -*- coding: utf-8 -*-

"""
Reading and caching command-line completion strings from
a database schema.

:copyright: (c) 2012 by Jay Sweeney.
:license: see LICENSE for more details.
"""

import atexit
from collections import defaultdict, namedtuple
import datetime
from datetime import timedelta
from dateutil import parser
import multiprocessing
from multiprocessing.pool import ThreadPool
import os
import Queue
import sqlite3
import threading

import sqlalchemy as sa
from sqlalchemy.engine.url import URL
from IPython.utils.path import locate_profile

CACHE_MAX_AGE = 60 * 10  # invalidate connection metadata if
                         # it is older than CACHE_MAX_AGE


ForeignKey = namedtuple('ForeignKey', 'table columns reftable refcolumns')


def fk_as_join(fk):
    """Return a string join statment from a ForeignKey object.

    Args:
        fk: a ForeignKey object
    Returns:
        string: "a inner join b on a.f = b.g..."
    """
    joinstr = '%s inner join %s on ' % (fk.reftable, fk.table)
    sep = ''
    for idx, col in enumerate(fk.columns):
        joinstr += sep + '%s.%s = %s.%s' % (
            fk.reftable, fk.refcolumns[idx],
            fk.table, col)
        sep = ' and '
    return joinstr


class MetaData(object):

    def __init__(self):
        self.isempty = True
        self.reflecting = False
        self.created = datetime.datetime(datetime.MINYEAR, 1, 1)
        self._tables = set()
        self._fields = set()
        self._dottedfields = set()
        self._types = dict()
        self._foreign_keys = []

    def get_fields(self, table=None):
        if table:
            return [df.split('.')[1] for df in self.dottedfields
                    if df.startswith(table + '.')]
        return self.fields

    def get_dottedfields(self, table=None):
        if table:
            return [df for df in self.dottedfields
                    if df.startswith(table + '.')]
        return self.dottedfields

    def tables_referencing(self, table):
        """Return a set of table names reference a given table name.

        Args:
            table: Name of table.
        Returns:
            Set of table names that refence input table name.
        """
        refs = set()
        for fk in self.foreign_keys:
            if table == fk.table:
                refs.add(fk.reftable)
            elif table == fk.reftable:
                refs.add(fk.table)
        return refs

    def fields_referencing(self, table, field=None):
        refs = []
        for fk in self.foreign_keys:
            if table == fk.reftable:
                if field and field in fk.refcolumns:
                    refs.append(fk)
                elif not field:
                    refs.append(fk)
        return refs

    def get_all_joins(self, table):
        """Return all possible joins (fks) to and from a table.

        Args:
            table - return joins for table
        Returns:
            list fk's that represent joins to or from the given table.
        """
        refs = []
        for fk in self.foreign_keys:
            if table in (fk.reftable, fk.table):
                refs.append(fk)
        return refs

    def get_joins(self, t1, t2):
        """Return foreign_keys that can join two tables.

        Args:
            t1: First table name.
            t2: Second table name.
        Returns:
            A List of ForeignKey named tuples between the two tables.
        """
        joins = []
        for fk in self.foreign_keys:
            if t1 in (fk.table, fk.reftable) and \
                    t2 in (fk.table, fk.reftable):
                joins.append(fk)
        return joins

    @property
    def tables(self):
        return self._tables

    @tables.setter
    def tables(self, value):
        self._tables = value

    @property
    def fields(self):
        return self._fields

    @fields.setter
    def fields(self, value):
        self._fields = value

    @property
    def dottedfields(self):
        return self._dottedfields

    @dottedfields.setter
    def dottedfields(self, value):
        self._dottedfields = value

    @property
    def types(self):
        return self._types

    @types.setter
    def types(self, value):
        self._types = value

    @property
    def foreign_keys(self):
        return self._foreign_keys

    @foreign_keys.setter
    def foreign_keys(self, value):
        self._foreign_keys = value

    def __getitem__(self, key):
        # XXX: temporary back-compat hack
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)


class CompletionDataAccessor(object):
    '''reads and writes db-completion data from/to an sqlite db'''

    pool = ThreadPool(multiprocessing.cpu_count() * 2)
    dburl = 'sqlite:////%s' % os.path.join(locate_profile(), 'ipydb.sqlite')

    def __init__(self):
        self.metadata = defaultdict(self._meta)
        self.db = sa.engine.create_engine(self.dburl)
        self.create_schema(self.db)
        self._sa_metadata = None
        self.save_thread = MetadataSavingThread(self)
        self.save_thread.start()

    def _meta(self):
        return MetaData()

    def sa_metadata():
        def fget(self):
            meta = getattr(self, '_sa_metadata', None)
            if meta is None:
                self._sa_metadata = sa.MetaData()
            return self._sa_metadata
        return locals()
    sa_metadata = property(**sa_metadata())

    def get_metadata(self, db, noisy=False, force=False):
        db_key = self.get_db_key(db.url)
        metadata = self.metadata[db_key]
        if metadata.isempty:  # XXX: what if schema exists, but is empty?!
            self.read(db_key)  # XXX is this slow? use self.pool.apply_async?
            metadata.isempty = False
        now = datetime.datetime.now()
        if (force or metadata.isempty or (now - metadata['created']) >
                timedelta(seconds=CACHE_MAX_AGE)) \
                and not metadata['reflecting']:
            if noisy:
                print "Reflecting metadata..."
            metadata['reflecting'] = True

            def printtime(x):
                pass
                #print "completed in %.2s" % (time.time() - t0)
            self.pool.apply_async(self.reflect_metadata,
                                  (db,), callback=printtime)
        return metadata

    def reflect_metadata(self, target_db):
        db_key = self.get_db_key(target_db.url)
        table_names = target_db.table_names()
        self.pool.map(
            self.reflect_table,
            ((target_db, db_key, tablename) for tablename
             in sorted(table_names)))
        self.metadata[db_key]['created'] = datetime.datetime.now()
        self.metadata[db_key]['reflecting'] = False

        # write to database.
        # self.write_all(db_key)

    def reflect_table(self, arg):
        target_db, db_key, tablename = arg  # XXX: this sux
        metadata = self.sa_metadata  # XXX: not threadsafe
        self.sa_metadata.bind = target_db
        t = sa.Table(tablename, metadata, autoload=True)
        tablename = t.name.lower()
        self.metadata[db_key]['tables'].add(tablename)
        self.metadata[db_key].isempty = False
        fks = {}
        for col in t.columns:
            fieldname = col.name.lower()
            dottedname = tablename + '.' + fieldname
            self.metadata[db_key]['fields'].add(fieldname)
            self.metadata[db_key]['dottedfields'].add(dottedname)
            self.metadata[db_key]['types'][dottedname] = str(col.type)
            constraint_name, pos, reftable, refcolumn = \
                self._get_foreign_key_info(col)
            if refcolumn:
                #print '%s.%s -> %s.%s' % (t.name, col.name,
                                          #reftable, refcolumn)
                if constraint_name not in fks:
                    fks[constraint_name] = {
                        'table': tablename,
                        'columns': [],
                        'referenced_table': reftable,
                        'referenced_columns': []
                    }
                fks[constraint_name]['columns'].append(col.name)
                fks[constraint_name]['referenced_columns'].append(refcolumn)
        all_fks = self.metadata[db_key].foreign_keys
        for name, dct in fks.iteritems():
            fk = ForeignKey(dct['table'], dct['columns'],
                            dct['referenced_table'], dct['referenced_columns'])
            try:
                all_fks.remove(fk)
            except ValueError:
                pass
            all_fks.append(fk)
        write_queue.put_nowait((db_key, t))

    def get_db_key(self, url):
        '''minimal unique key for describing a db connection'''
        return str(URL(url.drivername, url.username, host=url.host,
                   port=url.port, database=url.database))

    def read(self, db_key):
        fks = {}
        result = self.db.execute("""
            select
                t.db_key,
                t.name as tablename,
                f.name as fieldname,
                f.type as type,
                constraint_name,
                position_in_constraint,
                referenced_table,
                referenced_column
            from dbtable t inner join dbfield f
                on f.table_id = t.id
            where
                t.db_key = :db_key
        """, dict(db_key=db_key))
        for r in result:
            self.metadata[db_key].isempty = False
            self.metadata[db_key]['tables'].add(r.tablename)
            self.metadata[db_key]['fields'].add(r.fieldname)
            dottedfield = '%s.%s' % (r.tablename, r.fieldname)
            self.metadata[db_key]['dottedfields'].add(dottedfield)
            self.metadata[db_key]['types'][dottedfield] = r.type
            if r.constraint_name:
                if r.constraint_name not in fks:
                    fks[r.constraint_name] = {
                        'table': r.tablename,
                        'columns': [],
                        'referenced_table': r.referenced_table,
                        'referenced_columns': []
                    }
                fks[r.constraint_name]['columns'].append(r.fieldname)
                fks[r.constraint_name]['referenced_columns'].append(
                    r.referenced_column)
        all_fks = []
        for name, dct in fks.iteritems():
            fk = ForeignKey(dct['table'],
                            dct['columns'],
                            dct['referenced_table'],
                            dct['referenced_columns'])
            all_fks.append(fk)
        self.metadata[db_key]['foreign_keys'] = all_fks
        result = self.db.execute("select max(created) as created from dbtable "
                                 "where db_key = :db_key",
                                 dict(db_key=db_key)).fetchone()
        if result[0]:
            self.metadata[db_key]['created'] = parser.parse(result[0])
        else:
            self.metadata[db_key]['created'] = datetime.datetime.now()

    def create_schema(self, sqconn):
            sqconn.execute("""
                create table if not exists dbtable (
                    id integer primary key,
                    db_key text not null,
                    name text not null,
                    created datetime not null default current_timestamp,
                    constraint db_table_unique
                        unique (db_key, name)
                        on conflict rollback
                )
            """)
            sqconn.execute("""
                create table if not exists dbfield (
                    id integer primary key,
                    table_id integer not null
                        references dbtable(id)
                        on delete cascade
                        on update cascade,
                    name text not null,
                    type text,
                    constraint_name text,
                    position_in_constraint int,
                    referenced_table text,
                    referenced_column text,
                    constraint db_field_unique
                        unique (table_id, name)
                        on conflict rollback
                )
            """)

    def flush(self):
        self.pool.terminate()
        self.pool.join()
        self.metadata = defaultdict(self._meta)
        self.delete_schema()
        self.create_schema(self.db)
        self.pool = ThreadPool(multiprocessing.cpu_count() * 2)

    def delete_schema(self):
        self.db.execute("""drop table dbfield""")
        self.db.execute("""drop table dbtable""")

    def tables(self, db):
        db_key = self.get_db_key(db.url)
        return self.metadata[db_key]['tables']

    def fields(self, db, table=None):
        if table:
            cols = []
            for df in self.dottedfields(db):
                tbl, fld = df.split('.')
                if tbl == table:
                    cols.append(fld)
            return cols
        db_key = self.get_db_key(db.url)
        return self.metadata[db_key]['fields']

    def dottedfields(self, db, table=None):
        db_key = self.get_db_key(db.url)
        all_fields = self.metadata[db_key]['dottedfields']
        if table:
            fields = []
            for df in all_fields:
                tbl, fld = df.split('.')
                if tbl == table:
                    fields.append(fld)
            all_fields = fields
        return all_fields

    def reflecting(self, db):
        db_key = self.get_db_key(db.url)
        return self.metadata[db_key]['reflecting']

    def types(self, db):
        return self.metadata[self.get_db_key(db.url)]['types']

    def write_all(self, db_key):
        with sqlite3.connect(self.dbfile) as sqconn:
            for dottedname in self.metadata[db_key]['dottedfields']:
                tablename, fieldname = dottedname.split('.', 1)
                type_ = self.metadata[db_key]['types'].get(dottedname, '')
                self.write(sqconn, db_key, tablename, fieldname, type_)

    def write(self, sqconn, db_key, table, field, type_=''):
        res = sqconn.execute(
            "select id from dbtable where db_key=:db_key and name=:table",
            dict(db_key=db_key, table=table))
        table_id = None
        row = res.fetchone()
        if row is not None:
            table_id = row[0]
        else:
            res = sqconn.execute(
                """insert into dbtable(db_key, name) values (
                    :db_key, :table)""",
                dict(db_key=db_key, table=table))
            table_id = res.lastrowid
        try:
            sqconn.execute(
                """insert into dbfield(table_id, name, type) values (
                    :table_id, :field, :type)""",
                dict(table_id=table_id, field=field, type=type_))
        except sqlite3.IntegrityError:  # exists
            sqconn.execute(
                """
                update dbfield set
                    type = :type
                where
                    table_id = :table_id
                    and name = :field""",
                dict(table_id=table_id, field=field, type=type_))

    def _get_foreign_key_info(self, column):
        constraint_name = None
        pos = None
        reftable = None
        refcolumn = None
        if len(column.foreign_keys):
            #  XXX: for now we pretend that there can only be one.
            fk = list(column.foreign_keys)[0]
            if fk.constraint:
                constraint_name = fk.constraint.name
                reftable, refcolumn = fk.target_fullname.split('.')
                pos = 1  # XXX: this is incorrect
        return constraint_name, pos, reftable, refcolumn

    def write_table(self, sqconn, db_key, table):
        """
        Writes information about a table to an sqlite db store.

        Args:
            table: an sa.Table instance
        """
        res = sqconn.execute(
            "select id from dbtable where db_key=:db_key and name=:table",
            dict(db_key=db_key, table=table.name))
        table_id = None
        row = res.fetchone()
        if row is not None:
            table_id = row[0]
        else:
            res = sqconn.execute(
                """insert into dbtable(db_key, name) values (
                    :db_key, :table)""",
                dict(db_key=db_key, table=table.name))
            table_id = res.lastrowid
        for column in table.columns:
            constraint_name, pos, reftable, refcolumn = \
                self._get_foreign_key_info(column)
            try:
                sqconn.execute(
                    """
                        insert into dbfield(
                            table_id,
                            name,
                            type,
                            constraint_name,
                            position_in_constraint,
                            referenced_table,
                            referenced_column
                        ) values (
                            :table_id,
                            :field,
                            :type_,
                            :constraint_name,
                            :pos,
                            :reftable,
                            :refcolumn
                        )
                    """,
                    dict(
                        table_id=table_id,
                        field=column.name,
                        type_=str(column.type),
                        constraint_name=constraint_name,
                        pos=pos,
                        reftable=reftable,
                        refcolumn=refcolumn))
            except sa.exc.IntegrityError:  # exists
                sqconn.execute(
                    """
                    update dbfield set
                        type = :type,
                        constraint_name = :constraint_name,
                        position_in_constraint = :pos,
                        referenced_table = :reftable,
                        referenced_column = :refcolumn
                    where
                        table_id = :table_id
                        and name = :field""",
                    dict(
                        table_id=table_id,
                        field=column.name,
                        type=str(column.type),
                        constraint_name=constraint_name,
                        pos=pos,
                        reftable=reftable,
                        refcolumn=refcolumn))


"""producer/consumer communication for
    metadata reading / sqlite writing threads"""
write_queue = Queue.Queue()


class MetadataSavingThread(threading.Thread):
    daemon = True
    stop_now = False

    def __init__(self, metadata):
        """
        Constructor

        Args:
            metadata: instance of CompletionDataAccessor
        """
        super(MetadataSavingThread, self).__init__()
        self.metadata = metadata
        atexit.register(self.stop)

    def run(self):
        try:
            while True:
                db_key, table = write_queue.get()
                if self.stop_now:
                    return
                self.metadata.write_table(self.metadata.db, db_key, table)
        except Exception as e:
            print(("The metadata saving thread hit an unexpected error (%s)."
                   "Metadata will not be written to the database.") % repr(e))

    def stop(self):
        """This can be called from the main thread to safely stop this thread.

        """
        self.stop_now = True
        write_queue.put_nowait((None, None))  # XXX: this seems very lame...
        self.join()
