#!/usr/bin/env python3
"""
DEV ONLY - load the provided MySQL dump into a local SQLite file so the app can
be run and tested without a MySQL server.

    python scripts/load_dump_sqlite.py dump-test-202512221924.sql D:/work/dev.db
    $env:DATABASE_URL = "sqlite:///D:/work/dev.db"    (use an ABSOLUTE path: Flask-SQLAlchemy resolves
                                                       relative SQLite paths against ./instance)
    python migrations/bul/add_match_outcome_type.py   # same migration you run on MySQL
    python app.py

The schema is created WITHOUT match.outcome_type, exactly like the real dump, so
the migration is exercised on the dump's data. Production uses MySQL: for that
see the README ("Database changes").
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def convert_mysql_string_escapes(sql: str) -> str:
    """MySQL \\' and \\\\ escapes -> standard SQL ('' and a plain backslash)."""
    out, in_str, i = [], False, 0
    while i < len(sql):
        c = sql[i]
        if in_str:
            if c == '\\' and i + 1 < len(sql):
                nxt = sql[i + 1]
                out.append("''" if nxt == "'" else {'n': '\n', 'r': '\r', 't': '\t', '0': '\0'}.get(nxt, nxt))
                i += 2
                continue
            if c == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_str = False
        elif c == "'":
            in_str = True
        out.append(c)
        i += 1
    return ''.join(out)


def main(dump_path: str, db_path: str) -> None:
    url = 'sqlite:///' + os.path.abspath(db_path).replace('\\', '/')
    os.environ['DATABASE_URL'] = url

    from sqlalchemy import create_engine
    import models

    if os.path.exists(db_path):
        os.remove(db_path)
    engine = create_engine(url)
    models.db.metadata.create_all(engine)
    # The real dump has no outcome_type column yet: remove it so the migration is meaningful.
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE `match` DROP COLUMN `outcome_type`')

    text = open(dump_path, encoding='utf-8').read()
    columns = {}
    for table, body in re.findall(r'CREATE TABLE `(\w+)` \((.*?)\n\)\s*ENGINE', text, flags=re.S):
        columns[table] = re.findall(r'^\s{2}`(\w+)` ', body, flags=re.M)

    loaded = {}
    with engine.begin() as conn:
        for table, values in re.findall(r'^INSERT INTO `(\w+)` VALUES (.*);$', text, flags=re.M):
            cols = ', '.join(f'`{c}`' for c in columns[table])
            conn.exec_driver_sql(
                f'INSERT INTO `{table}` ({cols}) VALUES {convert_mysql_string_escapes(values)}')
            loaded[table] = loaded.get(table, 0) + 1
    print(f'Created {db_path} ({url}); INSERT statements loaded per table: {loaded}')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
