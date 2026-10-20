# Manual setup for the TGCat MySQL target database

This is the verified setup flow for the current project state. It is based on the actual working MySQL installation in the current conda environment and on the project’s real Django settings.

The older notes in [sql_notes.txt](../sql_notes.txt) and [notes.txt](../notes.txt) are useful as historical clues, but they are not the current setup recipe. The project does not need the legacy schema rewrites that were attempted in earlier work.

## 1) Project requirements

For this project, the database should:

- contain the imported TGCat dump from [db/tgcat_2026-10-07:04:21:30_transfer_bkup.sql](tgcat_2026-10-07:04:21:30_transfer_bkup.sql)
- remain read-only for the Django app
- use a dedicated app user for normal reads
- use a separate admin user only for setup/import work
- match the DB name and credentials in the Django settings

## 2) Start the MySQL server from the conda environment

This project already includes a usable MySQL server in the active conda environment. The verified startup command is:

```bash
export CONDA_MYSQL_PREFIX="/Users/guenther/miniforge3/envs/tgdingo"
"$CONDA_MYSQL_PREFIX/bin/mysqld_safe" \
  --basedir="$CONDA_MYSQL_PREFIX" \
  --datadir="$CONDA_MYSQL_PREFIX/data" \
  --socket=/tmp/tgdingo-mysql.sock \
  --port=3307 \
  --pid-file="$CONDA_MYSQL_PREFIX/data/tgdingo.pid" \
  --log-error="$CONDA_MYSQL_PREFIX/data/tgdingo.err" \
  --user="$(id -un)" \
  --lc-messages-dir="$CONDA_MYSQL_PREFIX/share/mysql"
```

This is the working configuration for this environment. The earlier failures were caused by:

- starting against an uninitialized project-local data directory
- using the wrong socket path
- using a stale MySQL process that kept the InnoDB lock

Once it is running, confirm it is reachable:

```bash
mysql --socket=/tmp/tgdingo-mysql.sock -uroot -e 'SELECT VERSION();'
```

If you want to stop it later:

```bash
kill "$(cat "$CONDA_MYSQL_PREFIX/data/tgdingo.pid")"
```

## 3) Create the database and users once

The current project defaults are:

- database: `tgcat`
- app user: `tgcat_reader`
- admin user: `tgcat_admin`
- host: `localhost`
- port: `3307`
- socket: `/tmp/tgdingo-mysql.sock`

Create the database and accounts like this:

```sql
CREATE DATABASE tgcat CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'tgcat_admin'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON tgcat.* TO 'tgcat_admin'@'localhost';

CREATE USER 'tgcat_reader'@'localhost' IDENTIFIED BY 'change-me';
GRANT SELECT ON tgcat.* TO 'tgcat_reader'@'localhost';

FLUSH PRIVILEGES;
```

Note: `FLUSH PRIVILEGES;` triggers a MySQL deprecation warning in newer versions, but it is not a fatal error. It does not indicate a failed user creation.

## 4) Import the dump file

Use the admin user and the actual socket for this project:

```bash
mysql --socket=/tmp/tgdingo-mysql.sock -u tgcat_admin -p tgcat < 'db/tgcat_2026-10-07:04:21:30_transfer_bkup.sql'
```

This is the correct pattern because the dump file already contains the schema and the data. Do not create the schema by hand and then append the dump afterward.

## 5) Validate the import

After the import finishes, confirm that the key tables exist and contain data:

```sql
USE tgcat;
SHOW TABLES;
SELECT COUNT(*) FROM obsid;
SELECT COUNT(*) FROM source;
SELECT COUNT(*) FROM files;
SELECT COUNT(*) FROM spec_prop;
SELECT * FROM obsid LIMIT 3;
SELECT * FROM source LIMIT 3;
```

If those queries return rows, the import was successful.


## 8) Validate Django connection

Run:

```bash
cd /Users/guenther/projects/Chandraprojects/tgdingo

export TGDINGO_DB_NAME=tgcat
export TGDINGO_DB_USER=tgcat_reader
export TGDINGO_DB_PASSWORD='change-me'
export TGDINGO_DB_HOST=127.0.0.1
export TGDINGO_DB_PORT=3307

/Users/guenther/miniforge3/envs/tgdingo/bin/python manage.py check
```

A stronger validation is a small ORM query:

```bash
/Users/guenther/miniforge3/envs/tgdingo/bin/python manage.py shell -c "from catalog.models import Source; print(Source.objects.count())"
```

If this executes without a connection error, the database and user configuration are correct.

## 9) Recommended human-run sequence

Use this order for a clean manual setup:

1. start the conda MySQL server with the verified command above
2. connect as `root` with the project socket
3. create the `tgcat` database and admin/app users
4. import the dump file for the `tgcat_admin` user
5. validate the key tables and row counts
6. set the Django env vars to match the DB name, user, host, and port
7. run `manage.py check` or a small ORM query

This avoids the stale legacy steps and keeps the setup aligned with the actual project state and the current conda environment.

This avoids the brittle legacy steps from the older project attempt while still preparing the database in a way the app can actually use.



## 10) How to run the development server

```bash
cd /Users/guenther/projects/Chandraprojects/tgdingo
python manage.py runserver 0.0.0.0:8000
```