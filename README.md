# evwlint

Flag the views, procedures, triggers and SQL Server Agent job steps that read a versioned base
table instead of its `_evw` view. Reads two catalog exports. Opens no connection to anything.

A street centerline table was registered as versioned, and editors kept editing it every day.
A serving view, a nightly refresh procedure and an Agent job step still read the table by its
own name. With traditional versioning, an edit goes into two delta tables, the adds table and
the deletes table. The base table changes only when a compress moves rows into it. On this
geodatabase the compress was blocked by an old replica version, so the base table stopped
changing.

Nothing failed. The view returned rows, the procedure refreshed its copy, and the job reported
success every night. Every edit made after the last useful compress was missing from all three.
Two triggers on the same table had stopped too, also without an error. An edit saved through
versioning is written to the delta tables, so a trigger on the base table never saw it.

The fix was small: read `<table>_evw`, which merges the base table with the delta tables. The
hard part was the inventory. Nobody had a list of what read the base table, and a text search
for the table name also matched the comments that explained the fix.

```
$ python evwlint.py --self-test
evwlint self-test: synthetic catalog rows, no database, no network
--------------------------------------------------------------------
PASS  a view that reads a versioned base table is a READ finding  <-- pinned defect
PASS  and the finding names the _evw view to read instead
PASS  and fails the gate
PASS  and carries the line it was found on
PASS  the same view repointed at ROADS_evw is clean and passes
PASS  a view on an unversioned table is clean: no _evw, nothing to read
...
PASS  a base table named in a line comment is not a finding  <-- pinned defect
PASS  a base table named in a block comment is not a finding  <-- pinned defect
PASS  a nested block comment hides the base until its own close  <-- pinned defect
...
PASS  dynamic SQL in a string is UNVERIFIED, never silently clean  <-- pinned defect
PASS  and an UNVERIFIED item alone exits 3, not 0
...
PASS  an unqualified name shared with an unversioned table is AMBIGUOUS, not a guess  <-- pinned defect
...
PASS  an encrypted trigger on a base is still a TRIGGER: the parent column says so  <-- pinned defect
...
PASS  a base read beside its a<n> and d<n> tables is HANDROLLED, not READ  <-- pinned defect
PASS  and HANDROLLED never fails the gate
...
PASS  a module with no definition is ENCRYPTED, never clean  <-- pinned defect
...
PASS  a definition of exactly 4000 characters is TRUNCATED, and still scanned  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
PASS  and so is --app
PASS  main with no argv reads the arguments after the program name
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the ValueError it is looking for
PASS  a failed assertion turns the footer and the exit code red  <-- pinned defect
PASS  loading the file as a module defines the tool and runs nothing
--------------------------------------------------------------------
129 assertions, 0 failed
```

The full run prints all 129 assertions. The `...` lines are where this block is cut.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no database driver, no third-party package.
The tool reads CSV files that you export once with the queries below.

The self-test prints 129 assertions on Windows (Python 3.13) and on Ubuntu (Python 3.12.3). The
code uses no syntax newer than Python 3.6, but 3.12 is the oldest interpreter it was run on.

```
git clone https://github.com/uhsear/evwlint.git
```

## Usage

### 1. Export the objects of each database

Run this in each geodatabase. Several databases can go in one CSV, because each row names its
database.

```sql
SELECT DB_NAME() AS [database], s.name AS [schema], o.name AS [name],
       o.type AS [type],
       ISNULL(OBJECT_SCHEMA_NAME(o.parent_object_id) + '.'
              + OBJECT_NAME(o.parent_object_id), '') AS [parent],
       m.definition AS [definition]
FROM sys.objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
LEFT JOIN sys.sql_modules AS m ON m.object_id = o.object_id
WHERE o.type IN ('U', 'V', 'P', 'TR', 'FN', 'IF', 'TF')
  AND o.is_ms_shipped = 0;
```

### 2. Export the job steps (optional)

```sql
SELECT j.name AS job_name, s.step_id, s.step_name, s.subsystem,
       s.database_name, s.command
FROM msdb.dbo.sysjobs AS j
JOIN msdb.dbo.sysjobsteps AS s ON s.job_id = j.job_id;
```

### 3. Save each result as a CSV

A definition holds line breaks, so the CSV must quote its fields. `Export-Csv` does. This
Windows PowerShell 5.1 snippet needs no extra module:

```powershell
$cn = New-Object System.Data.SqlClient.SqlConnection "Server=YOURSERVER;Database=YOURDB;Integrated Security=true"
$da = New-Object System.Data.SqlClient.SqlDataAdapter((Get-Content -Raw objects.sql), $cn)
$t = New-Object System.Data.DataTable
[void]$da.Fill($t)
$t | Export-Csv objects.csv -NoTypeInformation -Encoding UTF8
```

If you use `Invoke-Sqlcmd`, add `-MaxCharLength 2147483647`. Without it, `Invoke-Sqlcmd` returns
at most 4,000 characters of each definition, and the rest of a long procedure is never scanned.
The tool reports a definition of exactly 4,000 characters as `TRUNCATED`.

### 4. Run it

```
python evwlint.py --self-test
python evwlint.py --objects objects.csv
python evwlint.py --objects objects.csv --jobs jobsteps.csv
python evwlint.py --objects objects.csv --skip gis.ROADS
python evwlint.py --objects objects.csv --out findings.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--objects` | none | The objects export. Required unless `--self-test`. |
| `--jobs` | none | The job steps export. |
| `--skip` | none | A versioned table to leave out, as `schema.table` or `database.schema.table`. Repeatable. |
| `--out` | none | Write the findings to this CSV. Needs `--apply`. |
| `--apply` | off | Actually write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

Column names are matched without case, and extra columns are ignored. A UTF-8 BOM and the
`#TYPE` line that Windows PowerShell's `Export-Csv` writes without `-NoTypeInformation` are both
skipped.

## What it reports

```
$ python evwlint.py --objects objects.csv --jobs jobsteps.csv
evwlint: objects.csv 17 object(s), jobsteps.csv 3 job step(s)
versioned tables (an _evw view is in the export): 2
scanned: 8 module(s), 3 job step(s); 4 Esri object(s) left alone

FINDINGS: these read or write a versioned base table
  READ       view gisdb.gis.RoadsServing (line 7)
             reads gis.ROADS, which misses every edit still in the delta tables; read gis.ROADS_evw instead
  READ       procedure gisdb.gis.refresh_lookup (line 12)
             reads gis.ROADS, which misses every edit still in the delta tables; read gis.ROADS_evw instead
  WRITE      procedure gisdb.gis.stamp_parcels (line 2)
             writes gis.PARCELS directly; edit through gis.PARCELS_evw instead
  TRIGGER    trigger gisdb.gis.trg_roads_ins
             is a trigger on gis.ROADS. A versioned edit is saved to the delta tables, so this trigger does not see it
  READ       job 'Nightly lookup refresh' step 2 'check count' (line 2)
             reads gis.ROADS, which misses every edit still in the delta tables; read gis.ROADS_evw instead

REVIEW: hand-written versioned reads, for a human to compare
  HANDROLLED view gisdb.gis.ParcelsHand (line 2)
             reads gis.PARCELS with delta tables a17 and d17 by hand; compare it with gis.PARCELS_evw

UNVERIFIED: the tool could not prove these clean
  ENCRYPTED  view gisdb.gis.RoadsLocked
             has no definition in the export: encrypted, or not visible to the login that exported it
  IN_STRING  procedure gisdb.gis.count_parcels (line 2)
             names gis.PARCELS inside a string: dynamic SQL or message text, not checked
  NOT_TSQL   job 'Nightly lookup refresh' step 3 'export' (line 1)
             the PowerShell step names gis.PARCELS, and the command is not T-SQL, so it was not parsed

VERDICT: 5 finding(s) in 5 object(s). They serve the base table without the edits in its delta tables.
```

This run is real. The two CSVs were exported from a SQL Server 2022 container with the queries
above and the PowerShell snippet, which used SQL authentication there in place of
`Integrated Security`. The database is synthetic. Its two `_evw` views are plain views
made by hand to stand in for the views Esri creates, because the tool reads only their names.

`RoadsServing` names `gis.ROADS` three times: in a nested block comment, in a line comment and in
its `FROM` clause. Only line 7, the `FROM` clause, is reported. The view `RoadsFixed`, which reads
`gis.ROADS_evw`, is not reported. Job step 1 runs `EXEC gis.refresh_lookup` and is not reported
itself; the procedure is.

| Class | Fails the gate | Meaning |
|---|---|---|
| `READ` | yes | A module or a T-SQL job step reads a versioned base table. |
| `WRITE` | yes | It inserts, updates, deletes, merges or truncates the base table directly. |
| `TRIGGER` | yes | A trigger is defined on a versioned base table. |
| `HANDROLLED` | no | It reads the base table beside an `a<n>` and a `d<n>` table of one registration id. Somebody wrote the versioned view by hand. Compare it with `_evw`. |
| `AMBIGUOUS` | no, exit 3 | A one-part name matches the versioned table and an unversioned table in another schema. |
| `IN_STRING` | no, exit 3 | The name is inside a string literal: dynamic SQL or message text. |
| `ENCRYPTED` | no, exit 3 | The definition is empty: the module is encrypted, or the exporting login cannot see it. |
| `TRUNCATED` | no, exit 3 | The definition or command is exactly 4,000 characters long. |
| `NOT_TSQL` | no, exit 3 | A PowerShell, CmdExec or other non-T-SQL job step names the table. |

A trigger is found from the `parent` column, not from its text. So an encrypted trigger on a
versioned table is still reported as `TRIGGER`.

For an `ENCRYPTED` module, SQL Server's own dependency view can still answer. In the same test
database, this query listed `RoadsLocked` as a reader of `gis.ROADS`:

```sql
SELECT OBJECT_SCHEMA_NAME(referencing_id) + '.' + OBJECT_NAME(referencing_id) AS referencing
FROM sys.sql_expression_dependencies
WHERE referenced_entity_name = 'ROADS';
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Proven clean: no finding, nothing unverified, and at least one versioned table was known. |
| 1 | At least one `READ`, `WRITE` or `TRIGGER` finding. |
| 2 | A file could not be read or written, or the CSV has a malformed quote. argparse also exits 2 on an unknown flag, such as `--ap`. |
| 3 | Not proven. Only unverified items, or the export holds no `_evw` view at all. |
| 64 | Usage error, including a missing column and a `--skip` that matches no versioned table. |

Exit 3 is not exit 0 on purpose. An export with no `_evw` view usually means the wrong database
or a login that cannot see the views. A check that reported "clean" over nothing would never go
red.

## How it decides

**What is versioned.** A table is versioned when the export holds a view with the same name plus
`_evw`, in the same database and schema. Esri: "The view that is created has the same name as the
table or feature class with _evw appended to the end", and versioned views "are automatically
created for tables or feature classes that are registered as versioned in ArcGIS 10.1 or later
releases" ([What is a versioned view?](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/what-is-a-versioned-view.htm)).

**Why the base table is stale.** Through a versioned view, "all the records in the base table are
selected and merged with records from the delta tables" (same page). The adds table "stores all
records inserted to or updated in the dataset", and the deletes table "records all deletions"
([versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)).
A query on the base table alone reads neither delta table.

**Why a write is a finding.** "To use SQL to edit data that is registered as versioned
(traditional), you must use a versioned view"
([Edit versioned data in SQL Server using SQL](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/edit-versioned-data-using-sql-sqlserver.htm)).

**Why a trigger is a finding.** An edit is stored in the adds and deletes tables, so a DML trigger
on the base table does not fire when the edit is saved.

**Hand-written views.** The adds and deletes tables are named `a<registration_id>` and
`d<registration_id>`
([Versioned tables in a geodatabase in SQL Server](https://resources.arcgis.com/en/help/main/10.2/002q/002q0000007v000000.htm)).
A module that reads the base table beside both tables of one number, in the same schema, is
`HANDROLLED`. Aliases named `a17` and `d17` do not count; the self-test pins that.

**Esri's own objects.** Objects in the `sde` schema, the `_evw` views, and triggers whose parent
is an `_evw` view are counted and left alone. They read the base table by design.

**Comments and strings.** Comments are removed first. T-SQL block comments nest: "Nested comments
are supported" ([Slash Star](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/slash-star-comment-transact-sql)).
A name inside a string literal is reported as `IN_STRING`. A double-quoted run that holds
whitespace is treated as a string, because under `QUOTED_IDENTIFIER OFF` it is one.

**Names.** A two-part or three-part name is matched exactly. A four-part name is on a linked
server and is not matched. A one-part name is matched only after `FROM`, `JOIN`, `INTO`,
`UPDATE`, `TABLE`, `MERGE`, `USING`, `INSERT` or `DELETE`, because anywhere else it is usually a
column. SQL Server can search more than one schema for a one-part name: "It takes less processing time
for the Database Engine to resolve object names if it doesn't have to search multiple schemas"
([CREATE PROCEDURE](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-procedure-transact-sql)).
The schemas it searches depend on a default schema that no export records. So when an unversioned table shares the name, the tool reports `AMBIGUOUS` and does not guess.
Names compare without case, as the default collations do.

**Catalog facts.** `sys.objects.type` is `char(2)`, so a view arrives as `"V "` with a trailing
space ([sys.objects](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-objects-transact-sql)).
The test export showed exactly that. `sys.sql_modules.definition` is `NULL` for an encrypted
module ([sys.sql_modules](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-sql-modules-transact-sql)).
`sysjobsteps.database_name` is the database "in which command is executed if subsystem is TSQL"
([dbo.sysjobsteps](https://learn.microsoft.com/en-us/sql/relational-databases/system-tables/dbo-sysjobsteps-transact-sql)).
A `USE` statement inside a step moves the step to that database. `Invoke-Sqlcmd -MaxCharLength`
has a default of 4,000 characters
([Invoke-Sqlcmd](https://learn.microsoft.com/en-us/powershell/module/sqlserver/invoke-sqlcmd)).

## The existing alternative

SQL Server already records which modules name a table:
[`sys.sql_expression_dependencies`](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-sql-expression-dependencies-transact-sql).
It is the engine's own parse, it needs no export, and it sees into encrypted modules. Use it
first if you have one table to check.

In the test database it listed five readers of `gis.ROADS` and `gis.PARCELS` that are not Esri's.
It did not list `count_parcels`, which names `gis.PARCELS` inside an `EXEC` string. It did not
list `trg_roads_ins`, the trigger defined on `gis.ROADS`. It does not cover Agent job steps,
because a job step is not a module. And it does not know which tables are versioned, so it lists
the `_evw` views, their triggers and the `sde` objects beside the real problems. This tool adds
those four things.

compressfloor names what holds the compress floor. It has no `_evw` handling.

A `gh search repos` for `evwlint`, for `_evw versioned view` and for `versioned view lint sde`
found no repository that does this check, on 2026-10-09. A code search for `_evw` found scripts
that read through `_evw` themselves. None of them checks other code.

## Limits

- Traditional versioning only. The tool knows a versioned table only by its `_evw` view, and
  Esri documents that view for data "registered to use traditional versioning". Branch
  versioned data is out of scope.
- ArcGIS 10.1 named the view `_vw`, not `_evw`. Those views are not recognised.
- The option to move edits to base "allows edits made to the default version of the geodatabase
  to be immediately moved from the delta tables" to the base tables
  ([versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)).
  The export does not show that option. Pass such a table to `--skip`. Each skip is listed with
  its count.
- One object is checked at a time. A job step that runs a flagged procedure is not linked to it.
- A synonym that points at a base table is not followed.
- A one-part name in a comma-separated `FROM a, b` list is not matched after the first table.
- `UPDATE alias SET ... FROM base alias` is reported as `READ`, not `WRITE`. It still fails the
  gate.
- A non-T-SQL job step is matched by name only, never parsed, and never fails the gate by itself.
- Identifiers compare without case. A database with a case-sensitive collation can hold two
  tables whose names differ only in case, and the tool treats them as one.
- The export sees only what the exporting login may see. A module that login cannot see is
  missing from the export, not reported.
- It opens no socket and imports no network or database module. There is no credential anywhere
  in it.
- `--out` is the only file it writes, and only with `--apply`.

## How it was tested

- The self-test: 129 assertions, on Windows Python 3.13 and on Ubuntu Python 3.12.3. Branch
  coverage is 100 percent.
- A SQL Server 2022 container with the synthetic database above. The objects and the jobs were
  exported with the Windows PowerShell 5.1 snippet, with and without `-NoTypeInformation`. The
  output is the run shown under "What it reports". The Linux run on the same CSVs printed the
  same text.
- A 5,301-character procedure, exported with `Invoke-Sqlcmd` from the SqlServer module 22.3.0.
  With the default it arrived as 4,000 characters, was reported as `TRUNCATED`, and its `READ`
  was lost. With `-MaxCharLength 2147483647` the `READ` was found on line 83.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [compressfloor](https://github.com/uhsear/compressfloor) - name what holds the compress floor.
  While the floor is held, the base table stops moving and every finding here serves stale rows.
- [gdbprune](https://github.com/uhsear/gdbprune) - delete stale leaf versions, printing the plan
  first. Pruning can release the floor that compressfloor names.
- [whowrites](https://github.com/uhsear/whowrites) - name every script that empties or rewrites
  a feature class. It looks at Python; this tool looks at SQL.
