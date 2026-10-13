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
PASS  a one-part name in a comma FROM list is a READ: a one-part name  <-- pinned defect
...
PASS  sp_executesql of a variable is DYNAMIC: exit 3, never clean  <-- pinned defect
...
PASS  an unqualified name shared with an unversioned table is AMBIGUOUS, not a guess  <-- pinned defect
...
PASS  an encrypted trigger on a base is still a TRIGGER: the parent column says so  <-- pinned defect
...
PASS  where the data owner is sde, its views, procedures and triggers are scanned; only the _evw view is Esri's  <-- pinned defect
...
PASS  a base read beside its a<n> and d<n> tables is HANDROLLED, not READ  <-- pinned defect
PASS  and HANDROLLED never fails the gate, but it is not clean: exit 3 and a verdict that does not say clean  <-- pinned defect
PASS  one a<n> and d<n> pair beside two bases excuses neither  <-- pinned defect
...
PASS  a module with no definition is ENCRYPTED, never clean  <-- pinned defect
...
PASS  a definition of exactly 4000 characters is TRUNCATED, and still scanned  <-- pinned defect
...
PASS  a run with no job export is not proven clean: a job step could read the base  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
PASS  and so is --app
PASS  main with no argv reads the arguments after the program name
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the ValueError it is looking for
PASS  a failed assertion turns the footer and the exit code red  <-- pinned defect
PASS  loading the file as a module defines the tool and runs nothing
--------------------------------------------------------------------
179 assertions, 0 failed
```

The full run prints all 179 assertions. The `...` lines are where this block is cut.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no database driver, no third-party package.
The tool reads CSV files that you export once with the queries below.

The self-test prints 179 assertions on Windows (Python 3.13), on Ubuntu (Python 3.12.3) and on
Windows with CPython 3.9.25, which is the oldest interpreter it was run on. All three runs used
`python -W error`. That run backs the "Python 3.9 or newer" claim above.

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

### 2. Export the job steps

The run exits 0 only when it was given a job export, because a job step is the place where the
stale read hides longest. If the server has no Agent jobs, pass a CSV that holds only the header
row.

```sql
SELECT j.name AS job_name, s.step_id, s.step_name, s.subsystem,
       s.database_name, s.command
FROM msdb.dbo.sysjobs AS j
JOIN msdb.dbo.sysjobsteps AS s ON s.job_id = j.job_id;
```

The job-step export can hold passwords and connection strings, because CmdExec and PowerShell
steps carry them in the command. The tool never prints a command. Even so, store the file outside
any repository, and delete it after the run. The `.gitignore` of this repository lists `*.csv`.

### 3. Save each result as a CSV

A definition holds line breaks, so the CSV must quote its fields, and it must keep those line
breaks. `Export-Csv` does both. This Windows PowerShell 5.1 snippet needs no extra module:

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
python evwlint.py --objects objects.csv --jobs jobsteps.csv
python evwlint.py --objects objects.csv --jobs jobsteps.csv --skip gis.ROADS
python evwlint.py --objects objects.csv --jobs jobsteps.csv --out findings.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--objects` | none | The objects export. Required unless `--self-test`. |
| `--jobs` | none | The job steps export. Without it the run cannot exit 0. |
| `--skip` | none | A versioned table to leave out, as `schema.table` or `database.schema.table`. Repeatable. |
| `--out` | none | Write the findings to this CSV. Needs `--apply`. It must not be an input file. |
| `--apply` | off | Actually write `--out`. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

Every parser option is spelled out in full. A prefix such as `--ap` is refused, so a shortened
write flag cannot write by accident.

Column names are matched without case, and extra columns are ignored. A UTF-8 BOM and the
`#TYPE` line that Windows PowerShell's `Export-Csv` writes without `-NoTypeInformation` are both
skipped. A UTF-16 file, such as `Export-Csv -Encoding Unicode` writes, is refused with exit 2.

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
The CSVs are not part of this repository, because the repository holds four files. The
self-test builds the same shapes from synthetic rows.

`RoadsServing` names `gis.ROADS` three times: in a nested block comment, in a line comment and in
its `FROM` clause. Only line 7, the `FROM` clause, is reported. The view `RoadsFixed`, which reads
`gis.ROADS_evw`, is not reported. Job step 1 runs `EXEC gis.refresh_lookup` and is not reported
itself; the procedure is.

| Class | Fails the gate | Meaning |
|---|---|---|
| `READ` | yes | A module or a T-SQL job step reads a versioned base table. |
| `WRITE` | yes | It inserts, updates, deletes, merges or truncates the base table directly. |
| `TRIGGER` | yes | A trigger is defined on a versioned base table. |
| `HANDROLLED` | no, exit 3 | It reads the base table beside an `a<n>` and a `d<n>` table of one registration id. Somebody wrote the versioned view by hand. Compare it with `_evw`. |
| `AMBIGUOUS` | no, exit 3 | A one-part name matches the versioned table and an unversioned table in another schema. |
| `IN_STRING` | no, exit 3 | The name is inside a string literal: dynamic SQL or message text. |
| `DYNAMIC` | no, exit 3 | The object runs SQL that it builds at run time, so no table name is visible. |
| `ENCRYPTED` | no, exit 3 | The definition is empty or the text `NULL`: the module is encrypted, or the exporting login cannot see it. |
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
| 0 | Proven clean: no finding, nothing unverified, a job export was given, and at least one versioned table was known. |
| 1 | At least one `READ`, `WRITE` or `TRIGGER` finding. |
| 2 | A file could not be read or written, or the CSV has a malformed quote or a NUL character. argparse also exits 2 on an unknown flag, such as `--ap`. |
| 3 | Not proven. Unverified items, a `HANDROLLED` read, no `_evw` view in the export, or no `--jobs` file. |
| 64 | Usage error, including a missing column, a `--skip` that matches no versioned table, and an `--out` that names an input file. |

Exit 3 is not exit 0 on purpose. An export with no `_evw` view usually means the wrong database
or a login that cannot see the views. A check that reported "clean" over nothing would never go
red. The same holds for a run with no job export, and for an object that runs SQL the tool cannot
read.

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
on the base table does not see the edit when it is saved. No Esri page cited here says this in so
many words. See Limits for what the finding rests on.

**Hand-written views.** The adds and deletes tables are named `a<registration_id>` and
`d<registration_id>`
([Versioned tables in a geodatabase in SQL Server](https://resources.arcgis.com/en/help/main/10.2/002q/002q0000007v000000.htm)).
A module that reads the base table beside both tables of one number, in the same schema, is
`HANDROLLED`. Aliases named `a17` and `d17` do not count; the self-test pins that. The export does
not say which registration id belongs to which table. So one pair beside two versioned bases
excuses neither, and both are `READ`.

**Esri's own objects.** In a database where no versioned table lives in the `sde` schema, objects
in the `sde` schema are counted and left alone. The same holds for the `_evw` views and for
triggers whose parent is an `_evw` view. They read the base table by design. Where the data owner
is `sde`, the user's own views, procedures and triggers live in that schema too, so there they are
scanned.

**Comments and strings.** Comments are removed first. T-SQL block comments nest: "Nested comments
are supported" ([Slash Star](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/slash-star-comment-transact-sql)).
A name inside a string literal is reported as `IN_STRING`. A double-quoted run that holds
whitespace is treated as a string, because under `QUOTED_IDENTIFIER OFF` it is one.

**Dynamic SQL.** `EXECUTE` accepts "@string_variable" or a constant string, joined with `+`, and
`sp_executesql` runs a statement "built dynamically"
([EXECUTE](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/execute-transact-sql),
[sp_executesql](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-executesql-transact-sql)).
The text that runs is not in the module. So `EXEC (@s)`, `EXEC ('a' + @t)`, `EXEC @proc` and
`sp_executesql @s` are `DYNAMIC`. A constant string alone, `EXEC @rc = proc` and a named
procedure are not, because the full text is in the file.

**Names.** A two-part or three-part name is matched exactly. A four-part name is on a linked
server and is not matched. A one-part name is matched only after `FROM`, `JOIN`, `INTO`,
`UPDATE`, `TABLE`, `MERGE`, `USING`, `INSERT` or `DELETE`, or after a comma in a `FROM` list,
because anywhere else it is usually a column. SQL Server can search more than one schema for a
one-part name: "It takes less processing time for the Database Engine to resolve object names if
it doesn't have to search multiple schemas"
([CREATE PROCEDURE](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-procedure-transact-sql)).
The schemas it searches depend on a default schema that no export records. So when an unversioned
table shares the name, the tool reports `AMBIGUOUS` and does not guess.
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

A `gh search repos` for `evwlint`, for `_evw versioned view`, for `versioned view lint sde` and for
`sde versioned view` found no repository that does this check, on 2026-10-09 and again on
2026-10-13. A code search for `_evw` found scripts that read through `_evw` themselves. None of
them checks other code.

## Limits

- Traditional versioning only. The tool knows a versioned table only by its `_evw` view, and
  Esri documents that view for data "registered to use traditional versioning". Branch
  versioned data is out of scope.
- ArcGIS 10.1 named the view `_vw`, not `_evw`. Those views are not recognised.
- The option to move edits to base "allows edits made to the default version of the geodatabase
  to be immediately moved from the delta tables" to the base tables
  ([versioning vocabulary](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/versioning-vocabulary.html)).
  The export does not show that option. Pass such a table to `--skip`. Each skip is listed with
  its count, and the verdict says the skipped readers were not checked.
- The trigger finding rests on the original incident and on the delta-table design, not on a
  test. The SQL Server 2022 test container has no SDE, so no trigger was fired under versioning.
  A compress moves rows into the base table with DML, and that move can fire such a trigger. So
  the claim is only that the trigger does not see edits at save time.
- Every trigger on a versioned base table is reported, including a disabled one, because
  `is_disabled` is not exported.
- One object is checked at a time. A job step that runs a flagged procedure is not linked to it.
- A synonym that points at a base table is not followed. A view that reads only the synonym gets
  no verdict. The self-test pins this.
- A one-part table name after `UPDATE TOP (n)` or `DELETE TOP (n)` is not matched, so
  `UPDATE TOP (5) ROADS SET a = 1` is not reported. Write the schema, or expect the miss.
- A one-part name after a comma in a `FROM` list is matched. The list ends at `WHERE`, `ORDER`,
  `GROUP`, `UNION` and a few other words. A word the tool does not know leaves the list open, and
  a later comma could then read a column that has a versioned table's name as that table.
- An `UPDATE` or `DELETE` that names its target through an alias, such as `DELETE r FROM
  gis.ROADS r` or `UPDATE alias SET ... FROM base alias`, is reported as `READ`, not `WRITE`.
  It still fails the gate.
- `DYNAMIC` has no `--skip`, because no table is named. An object that builds SQL at run time
  keeps the run at exit 3 until it is rewritten or a person accepts that exit. The tool sees only
  the `EXEC` and `sp_executesql` forms listed above.
- A non-T-SQL job step is matched by name only, never parsed, and never fails the gate by itself.
  A job step that runs a script file, for example `sqlcmd -i refresh.sql`, names no table, so it
  is not reported at all. The tool reads the command text, never the file.
- Identifiers compare without case. A database with a case-sensitive collation can hold two
  tables whose names differ only in case, and the tool treats them as one.
- The export sees only what the exporting login may see. A module that login cannot see is
  missing from the export, not reported.
- In a database where no versioned table lives in `sde`, everything in the `sde` schema is
  treated as Esri's and not scanned. Where the data owner is `sde`, Esri's own objects in that
  schema are scanned too. Whether any of them names a user table is not known, because this was
  not tested against a real `sde`-owned geodatabase. Use `--skip` if one does.
- A user view whose name ends in `_evw` is treated as Esri's and not scanned. It also marks the
  table with the shorter name as versioned, so a name collision can cause a false `READ`.
- The `parent` column is parsed as a plain dotted name. A table name with a space, a hyphen or a
  leading digit, for example `gis.Road Segments`, is not recognised as the trigger's parent. Such
  a trigger is reported as `READ` from its `ON` clause instead of `TRIGGER`, and an encrypted one
  is missed. `--skip` of such a table also leaves out that `READ`.
- A SQL export that lost its line breaks, as SSMS grid results saved to CSV can do by default,
  turns everything after the first `--` comment into comment. The tool then exits 0 on a procedure
  that reads the base table. Use the PowerShell snippet, or check that definitions keep their line
  breaks. This was not verified against SSMS here, only against the flattened text.
- SSMS may cut a saved definition at 65,535 characters. Only a cut at exactly 4,000 characters is
  reported as `TRUNCATED`. Check the length of your longest definition. `sqlcmd` writes the text
  `NULL` for an encrypted module, and the tool reads that as `ENCRYPTED`.
- An export with tables and `_evw` views but no modules and no job steps, for example a type
  filter that dropped `P`, `V` and `TR`, exits 0 with "scanned: 0 module(s)". Check the scanned
  count before you trust exit 0.
- A name that the output encoding cannot show prints as a backslash escape, such as `\u2265`, so a
  redirected run on Windows does not crash. The self-test checks this with a cp1252 stream, not
  with a real Windows console.
- It opens no socket and imports no network or database module. There is no credential anywhere
  in it.
- Apart from the scratch directory that `--self-test` creates and removes under the system temp
  path, `--out` is the only file it writes, and only with `--apply`. `--apply` replaces `--out` if
  it exists, and the tool refuses an `--out` that is the `--objects` or `--jobs` file.

## How it was tested

- The self-test: 179 assertions, on Windows Python 3.13, on Windows CPython 3.9.25 and on Ubuntu
  Python 3.12.3. Branch coverage is 100 percent. The two Windows runs and the Ubuntu run print
  the same lines.
- A SQL Server 2022 container with the synthetic database above. The objects and the jobs were
  exported with the Windows PowerShell 5.1 snippet, with and without `-NoTypeInformation`. The
  output is the run shown under "What it reports". The Linux run on the same CSVs printed the
  same text. The current code reproduces that output byte for byte from the saved CSVs.
- A procedure longer than 4,000 characters, exported with `Invoke-Sqlcmd` from the SqlServer
  module 22.3.0. With the default it arrived as 4,000 characters, was reported as `TRUNCATED`, and
  its `READ` was lost. With `-MaxCharLength 2147483647` the whole text arrived and the `READ` was
  found.
- Not run for this version: the SQL queries and the PowerShell snippet above, because no SQL
  Server was available when the dynamic-SQL, comma-list and `sde` changes were made.

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
