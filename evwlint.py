#!/usr/bin/env python
"""Flag views, procedures, triggers and job steps that read a versioned base table.

A table registered for traditional versioning keeps its edits in two delta
tables, the adds table and the deletes table. The base table holds only what a
compress has already moved into it. Esri's versioned view, named after the
table with _evw on the end, merges the base table with the delta tables, so it
shows the edits. A view, a procedure, a job step or a trigger written against
the base table does not see them. Nothing errors. Every job stays green, and
the rows it serves stop moving at the last compress that could move them.

This tool reads two catalog exports and names every module and job step that
reads or writes a versioned base table, and every trigger defined on one:

    python evwlint.py --self-test
    python evwlint.py --objects objects.csv --jobs jobsteps.csv
    python evwlint.py --objects objects.csv --jobs jobs.csv --skip gis.ROADS
    python evwlint.py --objects objects.csv --jobs jobs.csv --out f.csv --apply

A table is taken to be versioned when the export holds a view named after it
with _evw on the end. Comments are stripped before the scan, and a name inside
a string literal is reported as UNVERIFIED, because it is either dynamic SQL
or message text and the tool cannot tell which. SQL built at run time has no
table name to find, so it is UNVERIFIED too. Nothing is written without
--apply, and the tool opens no connection to anything.

Exit codes: 0 no object reads a versioned base table and nothing was left
unverified, 1 at least one object does, 2 a file could not be read or written,
3 not proven (unverified items, a hand-written read for a human to compare, no
versioned table in the export, or no --jobs file), 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import io
import os
import re
import runpy
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The suffix Esri gives a versioned view: the table name with _evw appended.
# ArcGIS 10.1 used _vw instead. Those views are not recognised (see README).
EVW_SUFFIX = "_evw"

# Invoke-Sqlcmd returns at most 4,000 characters of a text column unless
# -MaxCharLength says otherwise. A definition of exactly this length has
# probably been cut, and the cut part is the part nobody scanned.
TRUNCATE_AT = 4000

# Esri's own objects live in this schema. Their references to base and delta
# tables are the versioning machinery itself, not a defect.
ESRI_SCHEMA = "sde"

# sys.objects type codes that carry a SQL module, and how each one is named in
# the report.
MODULE_TYPES = {"V": "view", "P": "procedure", "TR": "trigger",
                "FN": "function", "IF": "function", "TF": "function"}

# =============================================================================

# Findings. Each one fails the gate.
READ = "READ"
WRITE = "WRITE"
TRIGGER = "TRIGGER"
# For a human to compare. Never fails the gate.
HANDROLLED = "HANDROLLED"
# The tool could not prove the object clean. Exit 3 when nothing else fails.
AMBIGUOUS = "AMBIGUOUS"
IN_STRING = "IN_STRING"
ENCRYPTED = "ENCRYPTED"
TRUNCATED = "TRUNCATED"
NOT_TSQL = "NOT_TSQL"
# SQL built at run time (EXEC of a variable, sp_executesql): no table name is
# visible, so the object cannot be shown clean.
DYNAMIC = "DYNAMIC"

FAILING = (READ, WRITE, TRIGGER)
REVIEWING = (HANDROLLED,)
UNPROVEN = (AMBIGUOUS, IN_STRING, ENCRYPTED, TRUNCATED, NOT_TSQL, DYNAMIC)

OBJECT_COLUMNS = ("database", "schema", "name", "type", "parent",
                  "definition")
JOB_COLUMNS = ("job_name", "step_id", "step_name", "subsystem",
               "database_name", "command")
OUT_COLUMNS = ("class", "where", "base", "line", "detail")

# Token kinds.
ID = "id"
DOT = "."
STR = "str"
OTHER = "x"

# A name right after one of these words sits where a table goes. Only there is
# a one-part name read as a table, because anywhere else it is a column.
TABLE_POSITION = ("from", "join", "into", "update", "table", "merge",
                  "using", "insert", "delete")
# The statement verbs that write the table named after them.
WRITE_VERBS = ("insert", "update", "delete", "merge")
# INSERT INTO, DELETE FROM, MERGE INTO and TRUNCATE TABLE write too.
WRITE_LEADS = ("into", "from", "table")
WRITE_LEAD_VERBS = ("insert", "delete", "merge", "truncate")

# These words end a FROM list, so a comma after one is not a table separator.
FROM_END = ("where", "group", "order", "having", "union", "except",
            "intersect", "option", "for", "select", "insert", "update",
            "delete", "merge", "set", "values", "declare", "exec", "execute",
            "if", "else", "begin", "end", "return", "print", "create",
            "alter", "drop", "truncate", "use", "while", "output")
# The dynamic-SQL words.
EXEC_WORDS = ("exec", "execute")

# Esri names a delta table a<registration_id> or d<registration_id>.
DELTA_NAME = re.compile(r"^([ad])([0-9]+)$")


class Finding(object):
    """One class of problem, in one object, about one base table."""

    def __init__(self, cls, where, base, line, detail):
        self.cls = cls
        self.where = where
        self.base = base
        self.line = line
        self.detail = detail

    def row(self):
        return {"class": self.cls, "where": self.where, "base": self.base,
                "line": "" if self.line is None else str(self.line),
                "detail": self.detail}


class Hit(object):
    """One name in one text that may mean a versioned base table."""

    def __init__(self, key, write, line, in_string, ambiguous):
        self.key = key
        self.write = write
        self.line = line
        self.in_string = in_string
        self.ambiguous = ambiguous


class Catalog(object):
    """The tables and versioned tables an objects export describes.

    Keys are (database, schema, name), case folded, because the default SQL
    Server collations compare identifiers without case.
    """

    def __init__(self, objects):
        self.by_name = {}
        self.versioned = set()
        self.display = {}
        for obj in objects:
            if obj["type"] == "U":
                self._add((obj["db"], obj["schema"], obj["name"]),
                          obj["schema_text"], obj["name_text"])
            elif obj["type"] == "V" and obj["name"].endswith(EVW_SUFFIX):
                # The view itself is the evidence. Its base table need not be
                # in the export for a reference to that base to be wrong.
                cut = len(EVW_SUFFIX)
                key = (obj["db"], obj["schema"], obj["name"][:-cut])
                self.versioned.add(key)
                self._add(key, obj["schema_text"], obj["name_text"][:-cut])

    def _add(self, key, schema_text, name_text):
        self.by_name.setdefault(key[2], set()).add(key)
        self.display.setdefault(key, "%s.%s" % (schema_text, name_text))

    def resolve(self, parts, db, table_position):
        """Versioned keys a dotted name may mean, and whether it is ambiguous.

        db None means the database is not known, so any database matches.
        """
        if len(parts) > 3:
            return [], False   # a four-part name is on a linked server
        if len(parts) == 3:
            db = parts[0] or db
        if len(parts) >= 2:
            schema, name = parts[-2], parts[-1]
        elif table_position:
            schema, name = "", parts[0]
        else:
            return [], False
        found = sorted(k for k in self.by_name.get(name, ())
                       if (db is None or k[0] == db)
                       and (not schema or k[1] == schema))
        hits = [k for k in found if k in self.versioned]
        # Without a schema the name resolves through the owner's default
        # schema, which no export records. When an unversioned table shares
        # the name, the tool cannot say which one the module meant.
        return hits, bool(hits) and not schema and len(hits) < len(found)


# --------------------------------------------------------------------- lexer

def _quoted(text, start, close):
    """The contents of a quoted run, doubled marks unescaped, and the index
    after its closing mark. An unterminated run takes the rest of the text."""
    parts = []
    j = start
    while True:
        k = text.find(close, j)
        if k < 0:
            parts.append(text[j:])
            return "".join(parts), len(text)
        if text.startswith(close, k + 1):
            parts.append(text[j:k + 1])
            j = k + 2
            continue
        parts.append(text[j:k])
        return "".join(parts), k + 1


def tokenize(text):
    """T-SQL as (kind, value, line) tokens, with every comment removed.

    Block comments nest in T-SQL, so /* a /* b */ c */ is one comment. A run
    in double quotes is an identifier when it holds no whitespace, and a
    string when it does, because under QUOTED_IDENTIFIER OFF it is a string.
    """
    out = []
    i = 0
    n = len(text)
    line = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            line += text.count("\n", i, j)
            i = j
        elif ch == "'" or (ch in "nN" and text.startswith("'", i + 1)):
            value, j = _quoted(text, i + (1 if ch == "'" else 2), "'")
            out.append((STR, value, line))
            line += text.count("\n", i, j)
            i = j
        elif ch in "[\"":
            value, j = _quoted(text, i + 1, "]" if ch == "[" else "\"")
            if ch == "\"" and any(c.isspace() for c in value):
                out.append((STR, value, line))
            else:
                out.append((ID, value.lower(), line))
            line += text.count("\n", i, j)
            i = j
        elif ch.isalpha() or ch in "_@#":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_@#$"):
                j += 1
            out.append((ID, text[i:j].lower(), line))
            i = j
        elif ch.isdigit():
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "."):
                j += 1
            out.append((OTHER, text[i:j], line))
            i = j
        else:
            out.append((DOT if ch == "." else OTHER, ch, line))
            i += 1
    return out


def names(tokens):
    """Each dotted name as (parts, line, word before it, word before that,
    listed).

    db..table, which names the default schema, comes out as three parts with
    an empty middle one. listed is True for a name that follows a comma in an
    open FROM list, as in FROM a, b, where b is in a table position too.
    """
    i = 0
    n = len(tokens)
    depth = 0
    open_from = set()   # the bracket depths whose FROM list is still open
    while i < n:
        kind, value, _line = tokens[i]
        if kind == OTHER:
            if value == "(":
                depth += 1
            elif value == ")":
                open_from.discard(depth)
                depth = max(depth - 1, 0)
            elif value == ";":
                open_from.discard(depth)
            i += 1
            continue
        if kind != ID:
            i += 1
            continue
        parts = [value]
        j = i + 1
        while j + 1 < n and tokens[j][0] == DOT:
            if tokens[j + 1][0] == ID:
                parts.append(tokens[j + 1][1])
                j += 2
            elif tokens[j + 1][0] == DOT:
                parts.append("")
                j += 1
            else:
                break
        listed = (depth in open_from and i >= 1
                  and tokens[i - 1][:2] == (OTHER, ","))
        if len(parts) == 1:
            if value == "from":
                open_from.add(depth)
            elif value in FROM_END:
                open_from.discard(depth)
        prev = tokens[i - 1][1] if i >= 1 and tokens[i - 1][0] == ID else None
        prev2 = tokens[i - 2][1] if i >= 2 and tokens[i - 2][0] == ID else None
        yield parts, tokens[i][2], prev, prev2, listed
        i = j


def parse_name(text):
    """The parts of the first dotted name in a short text, or []."""
    for parts, _line, _prev, _prev2, _listed in names(tokenize(text)):
        return parts
    return []


def is_write(prev, prev2):
    return prev in WRITE_VERBS or (prev in WRITE_LEADS
                                   and prev2 in WRITE_LEAD_VERBS)


def dynamic_line(tokens):
    """The line of the first statement that runs SQL built at run time, or
    None. EXEC (@s), EXEC ('a' + @t), EXEC @proc and sp_executesql @s are
    dynamic. EXEC ('a literal'), EXEC @rc = proc and a literal first argument
    to sp_executesql are not: the whole text is in the file.
    """
    n = len(tokens)

    def at(k):
        return tokens[k][:2] if k < n else (None, None)

    for i, (kind, value, line) in enumerate(tokens):
        if kind != ID:
            continue
        if value in EXEC_WORDS:
            if at(i + 1) == (OTHER, "("):
                if not (at(i + 2)[0] == STR and at(i + 3) == (OTHER, ")")):
                    return line
            elif (at(i + 1)[0] == ID and at(i + 1)[1].startswith("@")
                  and at(i + 2) != (OTHER, "=")):
                return line
        elif value == "sp_executesql":
            if at(i + 1)[0] != STR or at(i + 2) == (OTHER, "+"):
                return line
    return None


def scan(text, db, catalog, in_string=False, line_base=0):
    """Every name in a text that may mean a versioned base table.

    Returns the hits, the delta tables the text reads by hand as (letter,
    registration id, schema or None) triples, and the line of the first
    dynamic statement or None. A string literal is scanned too, and what it
    finds is marked in_string, because it is dynamic SQL or message text and
    the tool cannot tell which. A USE statement moves the database that
    one-part and two-part names resolve in.
    """
    hits = []
    deltas = set()
    tokens = tokenize(text)
    for kind, value, line in tokens:
        if kind == STR:
            sub, _, _ = scan(value, db, catalog, True, line_base + line - 1)
            hits.extend(sub)
    for parts, line, prev, prev2, listed in names(tokens):
        if prev == "use" and len(parts) == 1:
            db = parts[0]
            continue
        table_position = prev in TABLE_POSITION or listed
        match = DELTA_NAME.match(parts[-1])
        if match and (len(parts) >= 2 or table_position):
            deltas.add((match.group(1), match.group(2),
                        parts[-2] if len(parts) >= 2 else None))
        keys, ambiguous = catalog.resolve(parts, db, table_position)
        for key in keys:
            hits.append(Hit(key, is_write(prev, prev2), line_base + line,
                            in_string, ambiguous))
    dynamic = None if in_string else dynamic_line(tokens)
    if dynamic is not None:
        dynamic += line_base
    return hits, deltas, dynamic


def delta_pairs(deltas, schemas):
    """The registration ids that have both an a<n> and a d<n>, in sorted
    order. A pair must share the number and the schema, or have no schema."""
    ids = {}
    for letter, number, schema in deltas:
        if schema is None or schema in schemas:
            ids.setdefault(number, set()).add(letter)
    return sorted(n for n in ids if ids[n] == {"a", "d"})


def handrolled(key, deltas):
    """The registration id of an a<n> and d<n> pair this text reads beside
    the base, or None."""
    pairs = delta_pairs(deltas, (key[1],))
    return pairs[0] if pairs else None


# ------------------------------------------------------------------ analysis

class Report(object):
    """Everything one run found, and the counts that explain it."""

    def __init__(self):
        self.versioned = []
        self.findings = []
        self.skipped = {}
        self.modules = 0
        self.steps = 0
        self.jobs_checked = False
        self.esri = 0
        self._seen = set()

    def add(self, cls, where, base, line, detail):
        # One line per class, object and base: a view that reads a table on
        # forty lines is one finding, reported at the first of them.
        key = (cls, where, base)
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(Finding(cls, where, base, line, detail))

    def of(self, classes):
        return [f for f in self.findings if f.cls in classes]


def _evw(base):
    return base + EVW_SUFFIX


def _record(report, catalog, skip, where, hits, deltas, not_tsql=None,
            own_parent=None):
    """Turn the hits of one object into findings, most certain class first."""
    # The export does not say which registration id belongs to which base. So
    # a pair of delta tables excuses a read only when the object holds a pair
    # for every base it reads. One pair beside two bases excuses neither.
    plain = {h.key for h in hits
             if h.key not in skip and not not_tsql and not h.in_string
             and not h.ambiguous and not h.write and h.key != own_parent}
    pairs = delta_pairs(deltas, {k[1] for k in plain})
    pairable = len(pairs) >= len(plain)
    for hit in hits:
        base = catalog.display[hit.key]
        if hit.key in skip:
            report.skipped[base] = report.skipped.get(base, 0) + 1
        elif not_tsql:
            report.add(NOT_TSQL, where, base, hit.line,
                       "the %s step names %s, and the command is not T-SQL, "
                       "so it was not parsed" % (not_tsql, base))
        elif hit.in_string:
            report.add(IN_STRING, where, base, hit.line,
                       "names %s inside a string: dynamic SQL or message "
                       "text, not checked" % base)
        elif hit.ambiguous:
            report.add(AMBIGUOUS, where, base, hit.line,
                       "names %s without a schema, and an unversioned table "
                       "of that name exists too" % base.split(".")[-1])
        elif hit.write:
            report.add(WRITE, where, base, hit.line,
                       "writes %s directly; edit through %s instead"
                       % (base, _evw(base)))
        elif hit.key == own_parent:
            continue   # the ON clause of a trigger; TRIGGER already says it
        else:
            number = handrolled(hit.key, deltas) if pairable else None
            if number is None:
                report.add(READ, where, base, hit.line,
                           "reads %s, which misses every edit still in the "
                           "delta tables; read %s instead"
                           % (base, _evw(base)))
            else:
                report.add(HANDROLLED, where, base, hit.line,
                           "reads %s with delta tables a%s and d%s by hand; "
                           "compare it with %s" % (base, number, number,
                                                   _evw(base)))


def _dynamic(report, where, line):
    if line is not None:
        report.add(DYNAMIC, where, "", line,
                   "runs SQL it builds at run time (EXEC of a variable or a "
                   "concatenation, or sp_executesql), so the tables it reads "
                   "are not visible and it was not checked")


def _object_key(obj, parts):
    """The (database, schema, name) key a parent column names."""
    if len(parts) >= 2:
        return (obj["db"], parts[-2], parts[-1])
    return None


def analyse(objects, steps=None, skip=()):
    """The whole decision: read rows in, a Report out. No I/O here.

    steps None means no job export was given, and the run cannot be proven
    clean. An empty list means the server has no job steps.

    skip is a list of dotted names of versioned tables to leave out, such as
    tables registered with the option to move edits to base. A skip that
    matches no versioned table raises, because a typo would otherwise pass
    silently and skip nothing.
    """
    catalog = Catalog(objects)
    report = Report()
    report.jobs_checked = steps is not None
    # A database whose data owner is sde keeps its own views, procedures and
    # triggers in the sde schema, beside Esri's. Only there are they scanned.
    sde_owned = {k[0] for k in catalog.versioned if k[1] == ESRI_SCHEMA}
    report.versioned = sorted("%s.%s" % (k[0], catalog.display[k])
                              for k in catalog.versioned)

    skipped = set()
    for text in skip:
        parts = parse_name(text)
        if len(parts) not in (2, 3):
            raise ValueError("--skip %r is not a schema.table name" % text)
        db = parts[0] if len(parts) == 3 else None
        keys = [k for k in catalog.versioned if k[1:] == tuple(parts[-2:])
                and (db is None or k[0] == db)]
        if not keys:
            raise ValueError("--skip %r matches no versioned table in the "
                             "export" % text)
        skipped.update(keys)

    for obj in objects:
        kind = MODULE_TYPES.get(obj["type"])
        if kind is None:
            continue
        parent = _object_key(obj, parse_name(obj["parent"]))
        if (obj["schema"] == ESRI_SCHEMA and obj["db"] not in sde_owned) or (
                obj["type"] == "V" and obj["name"].endswith(EVW_SUFFIX)) or (
                parent and parent[2].endswith(EVW_SUFFIX)):
            # Esri's own: the sde schema, the _evw views, and the triggers on
            # them that write an edit into the delta tables.
            report.esri += 1
            continue
        report.modules += 1
        where = "%s %s.%s.%s" % (kind, obj["db_text"], obj["schema_text"],
                                 obj["name_text"])
        # Known from the parent column, so even an encrypted trigger is
        # caught.
        if obj["type"] == "TR" and parent in catalog.versioned:
            base = catalog.display[parent]
            if parent in skipped:
                report.skipped[base] = report.skipped.get(base, 0) + 1
            else:
                report.add(TRIGGER, where, base, None,
                           "is a trigger on %s. A versioned edit is saved to "
                           "the delta tables, so this trigger does not see "
                           "it" % base)
        definition = obj["definition"]
        # sqlcmd writes the text NULL for the NULL definition of an encrypted
        # module, and no module body is only that word.
        if not definition.strip() or definition.strip().upper() == "NULL":
            report.add(ENCRYPTED, where, "", None,
                       "has no definition in the export: encrypted, or not "
                       "visible to the login that exported it")
            continue
        if len(definition) == TRUNCATE_AT:
            report.add(TRUNCATED, where, "", None,
                       "the definition is exactly %d characters, where "
                       "Invoke-Sqlcmd cuts text by default; re-export with "
                       "-MaxCharLength" % TRUNCATE_AT)
        hits, deltas, dynamic = scan(definition, obj["db"], catalog)
        _dynamic(report, where, dynamic)
        _record(report, catalog, skipped, where, hits, deltas,
                own_parent=parent if obj["type"] == "TR" else None)

    for step in steps or ():
        report.steps += 1
        where = "job '%s' step %s '%s'" % (step["job_name"], step["step_id"],
                                           step["step_name"])
        command = step["command"]
        if len(command) == TRUNCATE_AT:
            report.add(TRUNCATED, where, "", None,
                       "the command is exactly %d characters, where "
                       "Invoke-Sqlcmd cuts text by default; re-export with "
                       "-MaxCharLength" % TRUNCATE_AT)
        subsystem = step["subsystem"]
        if subsystem.upper() == "TSQL":
            hits, deltas, dynamic = scan(command, step["db"] or None, catalog)
            _dynamic(report, where, dynamic)
            _record(report, catalog, skipped, where, hits, deltas)
        else:
            hits, _, _ = scan(command, step["db"] or None, catalog)
            _record(report, catalog, skipped, where, hits, set(),
                    not_tsql=subsystem or "unnamed")
    return report


def gate(report):
    """0 proven clean, 1 a finding, 3 not proven."""
    if report.of(FAILING):
        return 1
    if (report.of(UNPROVEN) or report.of(REVIEWING) or not report.versioned
            or not report.jobs_checked):
        return 3
    return 0


def describe(report):
    """The report as printable lines."""
    out = []
    out.append("versioned tables (an _evw view is in the export): %d"
               % len(report.versioned))
    jobs = ("%d job step(s)" % report.steps if report.jobs_checked
            else "job steps not exported")
    out.append("scanned: %d module(s), %s; %d Esri object(s) left alone"
               % (report.modules, jobs, report.esri))
    for heading, classes in (
            ("FINDINGS: these read or write a versioned base table",
             FAILING),
            ("REVIEW: hand-written versioned reads, for a human to compare",
             REVIEWING),
            ("UNVERIFIED: the tool could not prove these clean", UNPROVEN)):
        found = report.of(classes)
        if found:
            out.append("")
            out.append(heading)
            for f in found:
                at = "" if f.line is None else " (line %d)" % f.line
                out.append("  %-10s %s%s" % (f.cls, f.where, at))
                out.append("             %s" % f.detail)
    if report.skipped:
        out.append("")
        out.append("SKIPPED by --skip, so not checked:")
        for base in sorted(report.skipped):
            out.append("  %s  %d reference(s)" % (base, report.skipped[base]))
    out.append("")
    failing = report.of(FAILING)
    if failing:
        out.append("VERDICT: %d finding(s) in %d object(s). They serve the "
                   "base table without the edits in its delta tables."
                   % (len(failing), len({f.where for f in failing})))
    elif not report.versioned:
        out.append("VERDICT: NOT PROVEN. The export holds no _evw view, so no "
                   "table is known to be versioned. Check the database and "
                   "the login that exported it.")
    elif not report.jobs_checked:
        out.append("VERDICT: NOT PROVEN. No --jobs export was given, so no "
                   "Agent job step was checked. Pass a CSV with only the "
                   "header row if the server has no jobs.")
    elif report.of(UNPROVEN) or report.of(REVIEWING):
        out.append("VERDICT: NOT PROVEN. No finding, but %d item(s) could not "
                   "be checked or need a human."
                   % len(report.of(UNPROVEN) + report.of(REVIEWING)))
    elif report.skipped:
        out.append("VERDICT: no object reads a versioned base table, apart "
                   "from the SKIPPED ones above.")
    else:
        out.append("VERDICT: no object reads a versioned base table.")
    return out


# ---------------------------------------------------------------- self-test

def _obj(db, schema, name, type_, definition="", parent=""):
    """One objects row, as the reader returns it."""
    return {"db": db.lower(), "schema": schema.lower(), "name": name.lower(),
            "db_text": db, "schema_text": schema, "name_text": name,
            "type": type_.strip().upper(), "parent": parent,
            "definition": definition}


def _step(job, step_id, name, subsystem, db, command):
    return {"job_name": job, "step_id": str(step_id), "step_name": name,
            "subsystem": subsystem, "db": db.lower(), "command": command}


def self_test():
    """Assertions over the decision core and the CLI. Synthetic rows only."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def base_catalog(*extra):
        rows = [_obj("gisdb", "gis", "ROADS", "U"),
                _obj("gisdb", "gis", "ROADS_evw", "V", "SELECT 1"),
                _obj("gisdb", "gis", "PARCELS", "U"),
                _obj("gisdb", "gis", "PARCELS_evw", "V", "SELECT 1"),
                _obj("gisdb", "gis", "LOOKUP", "U")]
        return rows + list(extra)

    def run(definition, type_="V", name="Thing", parent="", steps=(),
            skip=()):
        return analyse(base_catalog(
            _obj("gisdb", "gis", name, type_, definition, parent)),
            steps, skip)

    def classes(report):
        return [f.cls for f in report.findings]

    print("evwlint self-test: synthetic catalog rows, no database, no network")
    print("-" * 68)

    # ---- the defect itself
    r = run("CREATE VIEW gis.Thing AS SELECT * FROM gis.ROADS")
    check(classes(r) == [READ],
          "a view that reads a versioned base table is a READ finding"
          "  <-- pinned defect")
    check("gis.ROADS_evw" in r.findings[0].detail,
          "and the finding names the _evw view to read instead")
    check(gate(r) == 1, "and fails the gate")
    check(r.findings[0].line == 1, "and carries the line it was found on")
    r = run("CREATE VIEW gis.Thing AS SELECT * FROM gis.ROADS_evw")
    check(classes(r) == [] and gate(r) == 0,
          "the same view repointed at ROADS_evw is clean and passes")
    r = run("CREATE VIEW gis.Thing AS SELECT * FROM gis.LOOKUP")
    check(classes(r) == [] and gate(r) == 0,
          "a view on an unversioned table is clean: no _evw, nothing to read")
    r = run("SELECT r.a, p.b FROM gis.ROADS r JOIN gis.PARCELS p ON 1=1")
    check(sorted(f.base for f in r.findings) == ["gis.PARCELS", "gis.ROADS"],
          "two versioned bases in one view are two findings")
    r = run("SELECT * FROM gis.ROADS\nUNION ALL\nSELECT * FROM gis.ROADS")
    check(len(r.findings) == 1,
          "one base read twice in one object is one finding, not two")

    # ---- comments and strings: where a naive grep goes wrong
    r = run("-- the base gis.ROADS only moves at compress\n"
            "SELECT * FROM gis.ROADS_evw")
    check(classes(r) == [],
          "a base table named in a line comment is not a finding"
          "  <-- pinned defect")
    r = run("/* gis.ROADS cannot see these edits */ SELECT * FROM "
            "gis.ROADS_evw")
    check(classes(r) == [],
          "a base table named in a block comment is not a finding"
          "  <-- pinned defect")
    r = run("/* outer /* inner */ gis.ROADS still comment */ SELECT 1")
    check(classes(r) == [],
          "a nested block comment hides the base until its own close"
          "  <-- pinned defect")
    r = run("/* never closed gis.ROADS")
    check(classes(r) == [], "an unclosed block comment runs to the end")
    r = run("-- no newline after gis.ROADS")
    check(classes(r) == [], "a line comment at the very end is still one")
    r = run("EXEC('SELECT * FROM gis.ROADS')", "P")
    check(classes(r) == [IN_STRING],
          "dynamic SQL in a string is UNVERIFIED, never silently clean"
          "  <-- pinned defect")
    check(gate(r) == 3, "and an UNVERIFIED item alone exits 3, not 0")
    r = run("EXEC(N'DELETE FROM gis.ROADS')", "P")
    check(classes(r) == [IN_STRING], "an N'' string is scanned the same way")
    r = run("SELECT 'it''s gis.ROADS' AS note FROM gis.ROADS_evw")
    check(classes(r) == [IN_STRING],
          "a doubled quote does not end the string early")
    r = run("SELECT 'never closed gis.ROADS")
    check(classes(r) == [IN_STRING], "an unclosed string runs to the end")
    r = run("THROW 50001, 'ROADS_evw returned 0 rows', 1")
    check(classes(r) == [],
          "a message naming only the _evw view is not a finding")
    r = run("SELECT x\nFROM gis.LOOKUP\nWHERE y = 'a\nb'\nAND z IN "
            "(SELECT 1 FROM gis.ROADS)")
    check(r.findings[0].line == 5,
          "the line count survives a string that spans lines")
    r = run("SELECT 1 /* a\nb */ ,\n 'x' FROM [gis].[ROADS]")
    check(r.findings[0].line == 3,
          "and a block comment that spans lines")
    r = run("EXEC('SELECT 1\nFROM gis.ROADS')")
    check(r.findings[0].line == 2,
          "a hit inside a string reports the line it is on, not the "
          "string's first")

    # ---- quoting and name forms
    r = run("SELECT * FROM [gis].[ROADS]")
    check(classes(r) == [READ], "a bracketed name is matched")
    r = run("SELECT * FROM \"gis\".\"ROADS\"")
    check(classes(r) == [READ], "a double-quoted name is matched")
    r = run("SELECT * FROM [gis].[ROADS]]x]")
    check(classes(r) == [], "a ]] inside brackets is part of the name")
    r = run("SELECT \"FROM gis.ROADS\" AS c")
    check(classes(r) == [IN_STRING],
          "a double-quoted run with spaces is a string, as under "
          "QUOTED_IDENTIFIER OFF")
    r = run("SELECT * FROM GIS.roads")
    check(classes(r) == [READ],
          "names compare without case, as the default collations do")
    r = run("SELECT * FROM gisdb.gis.ROADS")
    check(classes(r) == [READ], "a three-part name in this database matches")
    r = run("SELECT * FROM otherdb.gis.ROADS")
    check(classes(r) == [],
          "a three-part name in another database does not match its "
          "namesake here")
    r = run("SELECT * FROM gisdb..ROADS")
    check(classes(r) == [READ],
          "db..table, the default schema form, is resolved by name")
    r = run("SELECT * FROM remote.gisdb.gis.ROADS")
    check(classes(r) == [],
          "a four-part name is on a linked server and is not matched here")
    r = run("SELECT * FROM ROADS")
    check(classes(r) == [READ],
          "an unqualified table name is matched when only the versioned "
          "table has it")
    r = run("SELECT ROADS FROM gis.LOOKUP")
    check(classes(r) == [],
          "an unqualified name outside a table position is a column, not "
          "a table  <-- pinned defect")
    r = analyse(base_catalog(_obj("gisdb", "dbo", "ROADS", "U"),
                             _obj("gisdb", "gis", "V1", "V",
                                  "SELECT * FROM ROADS")))
    check(classes(r) == [AMBIGUOUS],
          "an unqualified name shared with an unversioned table is "
          "AMBIGUOUS, not a guess  <-- pinned defect")
    r = analyse(base_catalog(_obj("gisdb", "dbo", "ROADS", "U"),
                             _obj("gisdb", "gis", "V1", "V",
                                  "SELECT * FROM dbo.ROADS")))
    check(classes(r) == [],
          "the unversioned namesake named with its schema is clean")
    r = run("SELECT #ROADS.a FROM #ROADS JOIN @ROADS ON 1=1")
    check(classes(r) == [], "a temp table or variable is never the base")
    r = run("SELECT 1.5 AS n, x.* FROM gis.LOOKUP x")
    check(classes(r) == [], "a decimal and x.* do not make a name")
    r = run("SELECT a.")
    check(classes(r) == [], "a name cut off after a dot does not break the scan")
    check(parse_name("db..t") == ["db", "", "t"],
          "db..t parses as three parts with an empty schema")
    check(parse_name("") == [], "an empty text holds no name")
    r = run("/**/SELECT * FROM gis.ROADS")
    check(classes(r) == [READ], "an empty block comment ends where it ends")
    r = run("SELECT [a\nb] FROM gis.ROADS")
    check(r.findings[0].line == 2,
          "the line count survives a bracketed name that spans lines")
    r = run("EXEC('gis.ROADS')", "P")
    check(classes(r) == [IN_STRING], "a string that starts with the name")
    r = run("EXEC(N'gis.ROADS')", "P")
    check(classes(r) == [IN_STRING], "and an N'' string that starts with it")
    r = run("SELECT 'x''gis.ROADS'")
    check(classes(r) == [IN_STRING],
          "and a name right after a doubled quote")
    check(TRUNCATE_AT == 4000,
          "the truncation length is the 4,000 the README promises")

    # ---- FROM lists: a name after a comma is in a table position too
    for sql, label in (
            ("SELECT * FROM gis.LOOKUP l,gis.ROADS r", "a two-part name"),
            ("SELECT * FROM gis.OTHER o, ROADS r", "a one-part name"),
            ("SELECT * FROM gis.A a, gis.B b, ROADS r", "the third table"),
            ("SELECT * FROM (SELECT 1 x) s, ROADS r", "after a subquery"),
            ("SELECT * FROM gis.LOOKUP l\n, ROADS r", "after a line break"),
            ("SELECT 1) FROM gis.LOOKUP l, ROADS r", "after a stray paren"),
            ("UPDATE x SET a = 1 FROM gis.LOOKUP x, ROADS r",
             "in an UPDATE ... FROM")):
        r = run(sql, "P")
        check(classes(r) == [READ] and r.findings[0].base == "gis.ROADS",
              "a one-part name in a comma FROM list is a READ: %s"
              "  <-- pinned defect" % label)
    for sql, label in (
            ("SELECT a, ROADS FROM gis.LOOKUP", "a select list"),
            ("SELECT * FROM gis.LOOKUP ORDER BY a, ROADS", "an ORDER BY"),
            ("SELECT * FROM gis.LOOKUP WHERE a IN (1, ROADS)", "a bracket"),
            ("SELECT * FROM gis.LOOKUP; SELECT 1, ROADS", "the next statement"),
            ("SELECT * FROM gis.LOOKUP SELECT 1, ROADS", "an unended one")):
        r = run(sql, "P")
        check(classes(r) == [],
              "a comma outside a FROM list makes no table position: %s"
              % label)
    r = run("UPDATE TOP (5) ROADS SET a = 1", "P")
    check(classes(r) == [],
          "a known miss, pinned so the README Limits stay true: a one-part "
          "name after UPDATE TOP (n)")

    # ---- dynamic SQL: no table name to find, so no clean verdict
    r = run("SET @s = N'INSERT gis.CopyOfRoads SELECT * FROM gis.' + "
            "QUOTENAME(@t);\nEXEC sp_executesql @s;", "P")
    check(classes(r) == [DYNAMIC] and gate(r) == 3
          and r.findings[0].line == 2,
          "sp_executesql of a variable is DYNAMIC: exit 3, never clean"
          "  <-- pinned defect")
    for sql, label in (
            ("EXEC (@s)", "EXEC of a variable in brackets"),
            ("EXECUTE (@s)", "EXECUTE"),
            ("EXEC('SELECT * FROM gis.' + @t)", "a concatenation"),
            ("EXEC @proc", "a procedure named by a variable"),
            ("EXEC sp_executesql", "sp_executesql with no argument"),
            ("EXEC sp_executesql N'SELECT * FROM gis.' + @t",
             "sp_executesql of a concatenation")):
        r = run(sql, "P")
        check(DYNAMIC in classes(r) and gate(r) == 3,
              "dynamic SQL is DYNAMIC: %s  <-- pinned defect" % label)
    for sql, label in (
            ("EXEC gis.refresh_lookup", "a named procedure"),
            ("EXEC @rc = gis.refresh_lookup", "a return value"),
            ("EXEC('SELECT 1')", "a whole literal"),
            ("EXEC sp_executesql N'SELECT 1'", "a literal for sp_executesql"),
            ("SELECT 'EXEC (@s)'", "the words inside a string"),
            ("EXEC", "a bare EXEC at the end")):
        r = run(sql, "P")
        check(classes(r) == [], "not dynamic: %s" % label)
    r = run("SELECT 1", steps=[_step("J", 1, "s", "TSQL", "gisdb",
                                     "EXEC (@s)")])
    check(classes(r) == [DYNAMIC] and "job 'J' step 1" in r.findings[0].where,
          "a T-SQL job step with dynamic SQL is DYNAMIC too")
    r = run("SELECT 1", steps=[_step("J", 1, "s", "PowerShell", "",
                                     "EXEC (@s)")])
    check(classes(r) == [], "and a PowerShell step is not read for it")

    # ---- writes
    for sql, label in (
            ("INSERT INTO gis.ROADS (a) VALUES (1)", "INSERT INTO"),
            ("INSERT gis.ROADS (a) VALUES (1)", "INSERT without INTO"),
            ("UPDATE gis.ROADS SET a = 1", "UPDATE"),
            ("DELETE FROM gis.ROADS", "DELETE FROM"),
            ("DELETE gis.ROADS", "DELETE without FROM"),
            ("TRUNCATE TABLE gis.ROADS", "TRUNCATE TABLE"),
            ("MERGE INTO gis.ROADS AS t USING gis.LOOKUP s ON 1=1", "MERGE")):
        r = run(sql, "P")
        check(classes(r) == [WRITE], "%s is a WRITE finding" % label)
    r = run("DELETE t FROM gis.LOOKUP t JOIN gis.ROADS r ON 1=1", "P")
    check(classes(r) == [READ],
          "a base joined into another table's DELETE is a READ")
    r = run("UPDATE x SET a = 1 FROM gis.ROADS x", "P")
    check(classes(r) == [READ],
          "UPDATE alias FROM base reads as READ; it still fails the gate")

    # ---- triggers
    trg = ("CREATE TRIGGER gis.t_roads ON gis.ROADS AFTER INSERT AS "
           "INSERT INTO gis.LOOKUP SELECT * FROM inserted")
    r = run(trg, "TR ", "t_roads", "gis.ROADS")
    check(classes(r) == [TRIGGER],
          "an AFTER trigger on a versioned base is a TRIGGER finding"
          "  <-- pinned defect")
    check("delta tables" in r.findings[0].detail,
          "and says why: versioned edits go to the delta tables")
    check(gate(r) == 1, "and fails the gate")
    r = run(trg.replace("ON gis.ROADS", "ON gis.LOOKUP"), "TR", "t_lookup",
            "gis.LOOKUP")
    check(classes(r) == [], "a trigger on an unversioned table is clean")
    r = run("CREATE TRIGGER t ON gis.ROADS AFTER DELETE AS "
            "DELETE FROM gis.ROADS WHERE 1=0", "TR", "t", "gis.ROADS")
    check(classes(r) == [TRIGGER, WRITE],
          "a trigger that also writes its own base is both")
    r = run("CREATE TRIGGER t ON gis.LOOKUP AFTER INSERT AS "
            "SELECT * FROM gis.ROADS", "TR", "t", "gis.LOOKUP")
    check(classes(r) == [READ],
          "a trigger on another table that reads a base is a READ")
    r = run("", "TR", "t", "gis.ROADS")
    check(classes(r) == [TRIGGER, ENCRYPTED],
          "an encrypted trigger on a base is still a TRIGGER: the parent "
          "column says so  <-- pinned defect")
    r = run("CREATE TRIGGER t ON DATABASE FOR CREATE_TABLE AS SELECT 1",
            "TR", "t", "")
    check(classes(r) == [], "a trigger with no parent table is not a TRIGGER")
    r = run("SELECT * FROM gis.ROADS", "TR", "v_roads_ins", "gis.ROADS_evw")
    check(classes(r) == [] and r.esri == 3,
          "a trigger on an _evw view is Esri's own, and left alone")

    # ---- Esri's own objects
    r = analyse(base_catalog(_obj("gisdb", "sde", "proc", "P",
                                  "SELECT * FROM gis.ROADS")))
    check(classes(r) == [] and r.esri == 3,
          "the sde schema and both _evw views are counted and left alone")
    r = run("SELECT * FROM gis.ROADS", "U", "SomeTable")
    check(r.modules == 0, "a table row carries no module and is not scanned")
    sde_owned = [_obj("gisdb", "sde", "ROADS", "U"),
                 _obj("gisdb", "sde", "ROADS_evw", "V", "SELECT 1"),
                 _obj("gisdb", "sde", "RoadsServing", "V",
                      "SELECT * FROM sde.ROADS"),
                 _obj("gisdb", "sde", "refresh", "P",
                      "INSERT INTO sde.Copy SELECT * FROM sde.ROADS"),
                 _obj("gisdb", "sde", "t_roads", "TR", "SELECT 1",
                      "sde.ROADS")]
    r = analyse(sde_owned, [])
    check(sorted(classes(r)) == [READ, READ, TRIGGER] and r.esri == 1
          and r.modules == 3,
          "where the data owner is sde, its views, procedures and triggers "
          "are scanned; only the _evw view is Esri's  <-- pinned defect")
    r = analyse(sde_owned + [_obj("other", "sde", "proc", "P",
                                  "SELECT * FROM sde.ROADS")], [])
    check(r.esri == 2,
          "and the sde schema of a database with no versioned sde table "
          "is still left alone")
    r = analyse(base_catalog(
        _obj("gisdb", "gis", "RoadsSyn", "SN"),
        _obj("gisdb", "gis", "UsesSyn", "V", "SELECT * FROM gis.RoadsSyn")),
        [])
    check(classes(r) == [] and r.modules == 1,
          "a synonym is not followed: a view that reads one gets no verdict "
          "(a documented limit)")

    # ---- hand-rolled multiversioned views
    hand = ("SELECT * FROM gis.ROADS b WHERE NOT EXISTS (SELECT 1 FROM "
            "gis.D17 d) UNION ALL SELECT * FROM gis.a17")
    r = run(hand)
    check(classes(r) == [HANDROLLED],
          "a base read beside its a<n> and d<n> tables is HANDROLLED, not "
          "READ  <-- pinned defect")
    check(gate(r) == 3 and describe(r)[-1].startswith("VERDICT: NOT PROVEN"),
          "and HANDROLLED never fails the gate, but it is not clean: exit 3 "
          "and a verdict that does not say clean  <-- pinned defect")
    check("a17 and d17" in r.findings[0].detail,
          "and names the delta tables it found")
    r = run("SELECT * FROM gis.ROADS r JOIN gis.PARCELS p ON 1=1 "
            "JOIN gis.a5 ON 1=1 JOIN gis.d5 ON 1=1")
    check(classes(r) == [READ, READ],
          "one a<n> and d<n> pair beside two bases excuses neither"
          "  <-- pinned defect")
    r = run("SELECT * FROM gis.ROADS r JOIN gis.PARCELS p ON 1=1 "
            "JOIN gis.a5 ON 1=1 JOIN gis.d5 ON 1=1 JOIN gis.a6 ON 1=1 "
            "JOIN gis.d6 ON 1=1")
    check(classes(r) == [HANDROLLED, HANDROLLED],
          "and a pair for each base excuses both")
    r = run("SELECT * FROM gis.ROADS a17 JOIN gis.LOOKUP d17 ON 1=1")
    check(classes(r) == [READ],
          "aliases named a17 and d17 do not demote a READ"
          "  <-- pinned defect")
    r = run("SELECT * FROM gis.ROADS JOIN gis.a17 ON 1=1 JOIN gis.d18 ON 1=1")
    check(classes(r) == [READ],
          "an a<n> and a d<m> with different numbers are not a pair")
    r = run("SELECT * FROM gis.ROADS JOIN other.a17 ON 1=1 "
            "JOIN other.d17 ON 1=1")
    check(classes(r) == [READ],
          "delta tables in another schema are not this base's")
    r = run("SELECT * FROM gis.ROADS JOIN a17 ON 1=1 JOIN d17 ON 1=1")
    check(classes(r) == [HANDROLLED],
          "unqualified delta tables in a table position count")
    r = run("UPDATE gis.ROADS SET a = 1 FROM gis.a17, gis.d17", "P")
    check(classes(r) == [WRITE],
          "delta tables never excuse a WRITE")
    check(handrolled(("db", "gis", "roads"),
                     {("a", "1", "gis"), ("d", "1", None)}) == "1",
          "handrolled() pairs a schema-qualified a<n> with a bare d<n>")
    check(handrolled(("db", "gis", "roads"),
                     {("a", "2", "gis"), ("d", "2", "gis"),
                      ("a", "1", "gis"), ("d", "1", "gis")}) == "1",
          "and reports the lowest registration id first")

    # ---- what the export could not show
    r = run("", "P")
    check(classes(r) == [ENCRYPTED],
          "a module with no definition is ENCRYPTED, never clean"
          "  <-- pinned defect")
    check(gate(r) == 3, "and exits 3")
    r = run("   \n ", "P")
    check(classes(r) == [ENCRYPTED], "a definition of only whitespace too")
    r = run("NULL", "P")
    check(classes(r) == [ENCRYPTED],
          "and the text NULL, which sqlcmd writes for an encrypted module"
          "  <-- pinned defect")
    body = "SELECT * FROM gis.ROADS "
    cut = (body * 400)[:TRUNCATE_AT]
    r = run(cut, "P")
    check(classes(r) == [TRUNCATED, READ],
          "a definition of exactly 4000 characters is TRUNCATED, and still "
          "scanned  <-- pinned defect")
    r = run(cut[:-1], "P")
    check(classes(r) == [READ], "3999 characters is not TRUNCATED")
    r = analyse([_obj("gisdb", "gis", "V1", "V", "SELECT * FROM gis.ROADS"),
                 _obj("gisdb", "gis", "ROADS", "U")])
    check(r.versioned == [] and gate(r) == 3,
          "an export with no _evw view proves nothing and exits 3"
          "  <-- pinned defect")
    r = analyse([_obj("gisdb", "gis", "ROADS_evw", "V", "SELECT 1"),
                 _obj("gisdb", "gis", "V1", "V", "SELECT * FROM gis.ROADS")])
    check(classes(r) == [READ],
          "an _evw view is evidence alone; the base row need not be exported")
    r = analyse([_obj("gisdb", "gis", "ROADS_evw", "V ", "SELECT 1"),
                 _obj("gisdb", "gis", "V1", "V ", "SELECT * FROM gis.ROADS")])
    check(classes(r) == [READ],
          "a char(2) type of 'V ' with its padding is still a view"
          "  <-- pinned defect")

    # ---- several databases
    two = [_obj("db1", "gis", "ROADS_evw", "V", "SELECT 1"),
           _obj("db2", "gis", "ROADS", "U"),
           _obj("db2", "gis", "V2", "V", "SELECT * FROM gis.ROADS"),
           _obj("db1", "gis", "V1", "V", "SELECT * FROM gis.ROADS")]
    r = analyse(two)
    check([f.where for f in r.findings] == ["view db1.gis.V1"],
          "a versioned table in one database does not condemn its namesake "
          "in another")
    check(r.versioned == ["db1.gis.ROADS"],
          "the versioned list carries the database of each table")

    # ---- --skip
    r = run("SELECT * FROM gis.ROADS JOIN gis.PARCELS ON 1=1",
            skip=["gis.ROADS"])
    check(classes(r) == [READ] and r.findings[0].base == "gis.PARCELS",
          "--skip leaves out the named table and only that one")
    check(r.skipped == {"gis.ROADS": 1},
          "and counts what it left out, so the skip is never silent")
    r = run("x", "TR", "t", "gis.ROADS", skip=["gisdb.gis.ROADS"])
    check(classes(r) == [] and r.skipped == {"gis.ROADS": 1},
          "a three-part --skip covers a TRIGGER on that table too")
    raises(lambda: run("SELECT 1", skip=["gis.ROADZ"]),
           "a --skip that matches no versioned table refuses the run"
           "  <-- pinned defect")
    raises(lambda: run("SELECT 1", skip=["ROADS"]),
           "a --skip without a schema is refused")
    raises(lambda: run("SELECT 1", skip=["otherdb.gis.ROADS"]),
           "a --skip in a database the export does not hold is refused")

    # ---- job steps
    steps = [_step("Nightly", 1, "refresh", "TSQL", "gisdb",
                   "INSERT INTO gis.LOOKUP SELECT * FROM gis.ROADS")]
    r = run("SELECT 1", steps=steps)
    check(classes(r) == [READ] and r.findings[0].where.startswith(
        "job 'Nightly' step 1"),
        "a T-SQL job step that reads a base is a READ finding")
    r = run("SELECT 1", steps=[_step("J", 1, "s", "TSQL", "master",
                                     "SELECT * FROM gis.ROADS")])
    check(classes(r) == [],
          "a step whose database is not the export's does not match")
    r = run("SELECT 1", steps=[_step("J", 1, "s", "TSQL", "master",
                                     "USE gisdb;\nSELECT * FROM gis.ROADS")])
    check(classes(r) == [READ],
          "USE inside a step moves it into the export's database"
          "  <-- pinned defect")
    r = run("SELECT 1", steps=[_step("J", 2, "ps", "PowerShell", "",
                                     "Invoke-Sqlcmd -Query \"SELECT * FROM "
                                     "gis.ROADS\"")])
    check(classes(r) == [NOT_TSQL] and gate(r) == 3,
          "a PowerShell step that names a base is NOT_TSQL and exits 3")
    r = run("SELECT 1", steps=[_step("J", 3, "c", "", "", "gis.ROADS")])
    check("unnamed" in r.findings[0].detail,
          "a step with no subsystem is still named in the report")
    r = run("SELECT 1", steps=[_step("J", 4, "t", "TSQL", "gisdb",
                                     "x" * TRUNCATE_AT)])
    check(classes(r) == [TRUNCATED], "a 4000-character job command too")
    r = run("SELECT 1", steps=[_step("J", 5, "s", "TSQL", "gisdb",
                                     "SELECT * FROM gis.ROADS")],
            skip=["gis.ROADS"])
    check(classes(r) == [] and r.skipped == {"gis.ROADS": 1},
          "--skip covers job steps")

    # ---- describe and gate
    r = run("SELECT * FROM gis.ROADS")
    text = "\n".join(describe(r))
    check("FINDINGS" in text and "VERDICT: 1 finding(s) in 1 object(s)"
          in text, "describe() prints the finding and the verdict")
    r = run("SELECT * FROM gis.ROADS_evw")
    check(describe(r)[-1] == "VERDICT: no object reads a versioned base "
          "table.", "a clean run says so in its last line")
    r = analyse([])
    check("NOT PROVEN" in describe(r)[-1] and "no _evw" in describe(r)[-1],
          "an empty export says NOT PROVEN, never clean")
    r = run(hand + " /* x */ ", steps=[_step("J", 1, "p", "CmdExec", "",
                                             "gis.ROADS")],
            skip=["gis.PARCELS"])
    lines = describe(r)
    check(any(x.startswith("REVIEW") for x in lines)
          and any(x.startswith("UNVERIFIED") for x in lines)
          and lines[-1].startswith("VERDICT: NOT PROVEN. No finding"),
          "REVIEW and UNVERIFIED get their own headings")
    r = run("SELECT * FROM gis.PARCELS", skip=["gis.PARCELS"])
    check(any(x.startswith("SKIPPED") for x in describe(r)),
          "a skipped table gets its own heading")
    check(gate(r) == 0 and "apart from the SKIPPED" in describe(r)[-1],
          "and the verdict says the skipped readers were not checked"
          "  <-- pinned defect")
    r = analyse(base_catalog(_obj("gisdb", "gis", "V1", "V",
                                  "SELECT * FROM gis.ROADS_evw")))
    check(gate(r) == 3 and not r.jobs_checked
          and describe(r)[-1].startswith("VERDICT: NOT PROVEN. No --jobs")
          and "job steps not exported" in describe(r)[1],
          "a run with no job export is not proven clean: a job step could "
          "read the base  <-- pinned defect")
    r = run("SELECT * FROM gis.ROADS")
    check(r.steps == 0 and r.modules == 1 and r.jobs_checked
          and r.esri == 2,
          "the run counts its modules, steps and Esri objects")
    r = run("SELECT 1", steps=[_step("J", 1, "a", "TSQL", "gisdb", "SELECT 1"),
                               _step("J", 2, "b", "TSQL", "gisdb", "SELECT 2")])
    check(r.steps == 2 and "2 job step(s)" in describe(r)[1],
          "and counts each job step once")
    check(Finding(READ, "w", "b", None, "d").row()["line"] == "",
          "a finding with no line writes an empty line cell")

    # ---- the command line
    tmp = tempfile.mkdtemp(prefix="evwlint-selftest-")

    def cli_section():

        def run_cli(argv):
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main(argv)
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue(), err.getvalue()

        def write(name, columns, rows, prefix=""):
            path = os.path.join(tmp, name)
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write(prefix)
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in rows:
                    writer.writerow(row)
            return path

        objects_rows = [
            ["gisdb", "gis", "ROADS", "U ", "", ""],
            ["gisdb", "gis", "ROADS_evw", "V ", "", "CREATE VIEW ..."],
            ["gisdb", "gis", "RoadsView", "V ", "",
             "CREATE VIEW gis.RoadsView AS\n-- base gis.ROADS is frozen\n"
             "SELECT * FROM gis.ROADS"],
            ["gisdb", "gis", "t_roads", "TR", "gis.ROADS",
             "CREATE TRIGGER gis.t_roads ON gis.ROADS AFTER INSERT AS SELECT 1"],
            ["gisdb", "gis", "Fixed", "V ", "", "SELECT * FROM gis.ROADS_evw"],
        ]
        objects_csv = write("objects.csv", OBJECT_COLUMNS, objects_rows)
        jobs_csv = write("jobs.csv", JOB_COLUMNS,
                         [["Nightly", "1", "load", "TSQL", "gisdb",
                           "SELECT * FROM gis.ROADS"]])
        none_csv = write("none.csv", JOB_COLUMNS, [])
        code, out, err = run_cli(["--objects", objects_csv, "--jobs", jobs_csv])
        check(code == 1, "the CLI exits 1 on a finding")
        check("view gisdb.gis.RoadsView (line 3)" in out,
              "and names the view and the line, past the comment that names "
              "the base")
        check("trigger gisdb.gis.t_roads" in out and "job 'Nightly' step 1" in out,
              "and the trigger and the job step")
        check("Fixed" not in out, "and leaves the repointed view out")
        check(out.startswith("evwlint: %s 5 object(s), %s 1 job step(s)"
                             % (objects_csv, jobs_csv)),
              "and the first line counts what it read")

        clean_csv = write("clean.csv", OBJECT_COLUMNS,
                          [objects_rows[0], objects_rows[1], objects_rows[4]])
        code, out, err = run_cli(["--objects", clean_csv, "--jobs", none_csv])
        check(code == 0 and "VERDICT: no object" in out
              and "0 job step(s)" in out,
              "a clean export with a header-only jobs file exits 0")
        code, out, err = run_cli(["--objects", clean_csv])
        check(code == 3 and "No --jobs export" in out,
              "and the same export with no jobs file exits 3, not 0"
              "  <-- pinned defect")

        bom_csv = write("bom.csv", OBJECT_COLUMNS, objects_rows, "\ufeff")
        code, out, err = run_cli(["--objects", bom_csv])
        check(code == 1 and "RoadsView" in out,
              "a UTF-8 BOM before the header is stripped  <-- pinned defect")
        typed_csv = write("typed.csv", OBJECT_COLUMNS, objects_rows,
                          "#TYPE System.Data.DataRow\n")
        code, out, err = run_cli(["--objects", typed_csv])
        check(code == 1 and "RoadsView" in out,
              "a #TYPE line from Windows PowerShell Export-Csv is skipped"
              "  <-- pinned defect")
        upper_csv = write("upper.csv", [c.upper() for c in OBJECT_COLUMNS] +
                          ["RowError"], [r + [""] for r in objects_rows])
        code, out, err = run_cli(["--objects", upper_csv])
        check(code == 1, "column names match without case, and extra columns "
                         "are ignored")
        big = "SELECT * FROM gis.ROADS_evw " + " " * 200000
        big_csv = write("big.csv", OBJECT_COLUMNS,
                        [objects_rows[1], ["gisdb", "gis", "Big", "P", "", big]])
        code, out, err = run_cli(["--objects", big_csv, "--jobs", none_csv])
        check(code == 0,
              "a definition over csv's 131072-character default limit is read"
              "  <-- pinned defect")

        code, out, err = run_cli(["--objects", objects_csv, "--jobs", none_csv,
                                  "--skip", "gis.ROADS"])
        check(code == 0 and "SKIPPED" in out,
              "--skip on the CLI leaves the table out and says so")
        code, out, err = run_cli(["--objects", objects_csv, "--skip", "gis.NOPE"])
        check(code == 64 and "matches no versioned table" in err,
              "a --skip that matches nothing is a usage error, exit 64")

        missing_csv = write("missing.csv", ["database", "schema", "name"], [])
        code, out, err = run_cli(["--objects", missing_csv])
        check(code == 64 and "parent, definition" in err and "It has:" in err,
              "missing columns are a usage error that names them")
        empty_csv = write("empty.csv", [], [])
        code, out, err = run_cli(["--objects", empty_csv])
        check(code == 64 and "(no columns at all)" in err,
              "an empty CSV is a usage error, not a clean run")
        code, out, err = run_cli(["--objects", objects_csv, "--jobs",
                                  missing_csv])
        check(code == 64 and "job_name" in err,
              "a jobs CSV without its columns is a usage error")
        code, out, err = run_cli(["--objects", os.path.join(tmp, "nope.csv")])
        check(code == 2, "an unreadable file exits 2, not 1")
        quote_csv = os.path.join(tmp, "quote.csv")
        with open(quote_csv, "w", newline="", encoding="utf-8") as handle:
            handle.write(",".join(OBJECT_COLUMNS) + "\n\"a\"b,x\n")
        code, out, err = run_cli(["--objects", quote_csv])
        check(code == 2 and "error:" in err,
              "a malformed quote is refused with exit 2, never read past"
              "  <-- pinned defect")
        nodef_csv = write("nodef.csv", OBJECT_COLUMNS,
                          [objects_rows[1], ["gisdb", "gis", "Short", "P"]])
        code, out, err = run_cli(["--objects", nodef_csv])
        check(code == 3 and "ENCRYPTED" in out,
              "a short row reads its missing cells as empty, and is ENCRYPTED")

        out_csv = os.path.join(tmp, "findings.csv")
        code, out, err = run_cli(["--objects", objects_csv, "--out", out_csv])
        check(code == 1 and not os.path.exists(out_csv) and "Check only" in out,
              "--out without --apply writes nothing  <-- pinned defect")
        code, out, err = run_cli(["--objects", objects_csv, "--out", out_csv,
                                  "--apply"])
        with open(out_csv, newline="", encoding="utf-8") as handle:
            written = list(csv.DictReader(handle))
        check(code == 1 and len(written) == 2
              and written[0]["class"] == READ and written[0]["line"] == "3",
              "--out --apply writes one row per finding with its line")
        code, out, err = run_cli(["--objects", objects_csv, "--out", tmp,
                                  "--apply"])
        check(code == 2 and "could not write" in err,
              "an --out that cannot be written exits 2")
        before = os.path.getsize(objects_csv)
        code, out, err = run_cli(["--objects", objects_csv, "--out",
                                  objects_csv, "--apply"])
        check(code == 64 and "names an input file" in err
              and os.path.getsize(objects_csv) == before,
              "an --out that is the --objects file is refused, and the "
              "input is untouched  <-- pinned defect")
        code, out, err = run_cli(["--objects", objects_csv, "--jobs", jobs_csv,
                                  "--out", jobs_csv])
        check(code == 64, "and so is one that is the --jobs file")
        utf16_csv = os.path.join(tmp, "utf16.csv")
        with open(utf16_csv, "w", newline="", encoding="utf-16") as handle:
            handle.write(",".join(OBJECT_COLUMNS) + "\r\n")
        code, out, err = run_cli(["--objects", utf16_csv])
        check(code == 2 and "NUL" in err and "UTF-8" in err,
              "a UTF-16 export is refused with exit 2 on every interpreter"
              "  <-- pinned defect")
        nul_csv = os.path.join(tmp, "nul.csv")
        with open(nul_csv, "wb") as handle:
            handle.write((",".join(OBJECT_COLUMNS) + "\n").encode("ascii")
                         + b"a,b,c,d,e,f\x00\n")
        code, out, err = run_cli(["--objects", nul_csv])
        check(code == 2 and "NUL" in err,
              "and so is a NUL byte inside a row")
        uni_csv = write("uni.csv", OBJECT_COLUMNS,
                        [objects_rows[1],
                         ["gisdb", "gis", "Road\u2265s", "V", "",
                          "SELECT * FROM gis.ROADS"]])
        raw = io.BytesIO()
        wrapper = io.TextIOWrapper(raw, encoding="cp1252", newline="")
        with contextlib.redirect_stdout(wrapper), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(["--objects", uni_csv, "--jobs", none_csv])
        wrapper.flush()
        check(code == 1 and b"Road\\u2265s" in raw.getvalue(),
              "a name the console code page cannot hold prints as an escape "
              "and the run still exits 1 by its finding, not by a traceback"
              "  <-- pinned defect")
        code, out, err = run_cli(["--objects", objects_csv, "--apply"])
        check(code == 64, "--apply without --out is a usage error")
        code, out, err = run_cli([])
        check(code == 64 and "--objects" in err,
              "no --objects is a usage error that names the flag")
        code, out, err = run_cli(["--objects", objects_csv, "--ap"])
        check(code == 2 and "unrecognized arguments: --ap" in err,
              "a unique prefix --ap of --apply is REFUSED by the parser"
              "  <-- pinned defect")
        code, out, err = run_cli(["--objects", objects_csv, "--out", out_csv,
                                  "--app"])
        check(code == 2 and "--app" in err, "and so is --app")

        argv_before = sys.argv
        try:
            sys.argv = ["evwlint.py", "--objects", clean_csv, "--jobs", none_csv]
            code, out, err = run_cli(None)
            check(code == 0,
                  "main with no argv reads the arguments after the program name")
        finally:
            sys.argv = argv_before

    try:
        cli_section()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- the harness itself
    before = (passed[0], len(failed))
    with contextlib.redirect_stdout(io.StringIO()):
        check(False, "x")
        raises(lambda: None, "x")
        raises(lambda: 1 / 0, "x")
    caught = len(failed) - before[1]
    del failed[before[1]:]
    check(caught == 3 and passed[0] == before[0],
          "check() and raises() really do record a failure"
          "  <-- pinned defect")
    raises(lambda: analyse([], (), ["x.y"]),
           "and raises() accepts the ValueError it is looking for")

    with contextlib.redirect_stdout(io.StringIO()) as sink:
        code = _tally(2, ["a broken check"])
    check(code == 1 and "3 assertions, 1 failed" in sink.getvalue()
          and "FAILED: a broken check" in sink.getvalue(),
          "a failed assertion turns the footer and the exit code red"
          "  <-- pinned defect")
    with contextlib.redirect_stdout(io.StringIO()):
        loaded = runpy.run_path(os.path.abspath(__file__),
                                run_name="evwlint_imported")
    check("analyse" in loaded,
          "loading the file as a module defines the tool and runs nothing")

    print("-" * 68)
    status = _tally(passed[0], failed)
    # Not only _tally(): a broken footer must not turn a failure green.
    return 1 if failed else status


def _tally(passed, failed):
    """Print the footer and return the self-test exit code."""
    total = passed + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _no_nul(lines):
    """The lines unchanged, or csv.Error at the first NUL. A UTF-16 file
    decoded as UTF-8 is full of them, and the csv module accepts a NUL on
    Python 3.11 and later, so without this the exit code would depend on the
    interpreter."""
    for text in lines:
        if "\0" in text:
            raise csv.Error("the file holds a NUL character, as a UTF-16 "
                            "file does; save the export as UTF-8")
        yield text


def read_csv(path, columns):
    """The rows of a CSV as dicts keyed by the lower-case column names given.

    Raises ValueError, a usage error, when a column is missing.
    """
    # The definitions are whole procedures. csv's default field limit is
    # 131072 characters, and one long procedure would stop the run.
    csv.field_size_limit(2 ** 31 - 1)
    # utf-8-sig, because Export-Csv -Encoding UTF8 in Windows PowerShell
    # writes a BOM, and read as plain UTF-8 the first column name would carry
    # it and read as missing.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        first = handle.readline()
        # Windows PowerShell's Export-Csv writes "#TYPE <class>" first unless
        # told -NoTypeInformation. That line is not the header.
        if not first.startswith("#TYPE"):
            handle.seek(0)
        # strict, so a stray quote stops the run instead of gluing two
        # definitions together and scanning neither as written.
        reader = csv.reader(_no_nul(handle), strict=True)
        header = next(reader, [])
        index = {name.strip().lower(): i for i, name in enumerate(header)}
        absent = [c for c in columns if c not in index]
        if absent:
            raise ValueError(
                "%s: columns not in the CSV: %s. It has: %s"
                % (path, ", ".join(absent),
                   ", ".join(header) if header else "(no columns at all)"))
        rows = []
        for raw in reader:
            rows.append({c: raw[index[c]] if index[c] < len(raw) else ""
                         for c in columns})
        return rows


def load_objects(path):
    rows = read_csv(path, OBJECT_COLUMNS)
    return [_obj(r["database"], r["schema"], r["name"], r["type"],
                 r["definition"], r["parent"]) for r in rows]


def load_steps(path):
    rows = read_csv(path, JOB_COLUMNS)
    return [_step(r["job_name"], r["step_id"], r["step_name"],
                  r["subsystem"], r["database_name"], r["command"])
            for r in rows]


def write_findings(path, report):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUT_COLUMNS))
        writer.writeheader()
        for f in report.findings:
            writer.writerow(f.row())


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="evwlint.py",
        description="Flag views, procedures, triggers and job steps that "
                    "read a versioned base table instead of its _evw view.",
        epilog="Reads catalog exports only. Nothing is written without "
               "--apply.",
        allow_abbrev=False,
    )
    ap.add_argument("--objects", help="sys.objects and sys.sql_modules "
                                      "export as CSV (see README)")
    ap.add_argument("--jobs", help="msdb job steps export as CSV. Without "
                                   "it the run cannot exit 0; pass a CSV "
                                   "with only the header if there are no "
                                   "jobs.")
    ap.add_argument("--skip", action="append", default=[],
                    metavar="SCHEMA.TABLE",
                    help="a versioned table to leave out, such as one "
                         "registered to move edits to base. Repeatable.")
    ap.add_argument("--out", help="path for the findings CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _tolerate_output_encoding():
    """Print a name the console code page cannot hold as an escape. Without
    this, a Unicode object name raises UnicodeEncodeError partway through the
    report, and the traceback exits 1, which a scheduler reads as a finding.
    A stream with no reconfigure() is left alone."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except AttributeError:
            continue


def _same_file(out, *inputs):
    return os.path.exists(out) and any(
        p and os.path.exists(p) and os.path.samefile(out, p) for p in inputs)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    _tolerate_output_encoding()

    if args.self_test:
        return self_test()
    if not args.objects:
        print("error: --objects is required. Use --self-test to verify the "
              "tool without it.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64

    if args.out and _same_file(args.out, args.objects, args.jobs):
        print("error: --out names an input file, and --apply would "
              "overwrite it.", file=sys.stderr)
        return 64

    try:
        objects = load_objects(args.objects)
        steps = load_steps(args.jobs) if args.jobs else None
        report = analyse(objects, steps, args.skip)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (IOError, OSError, csv.Error) as exc:
        # csv.Error too: a NUL or a bad quote is an unreadable input, and exit 1
        # would tell a scheduler that a finding exists.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    head = "evwlint: %s %d object(s)" % (args.objects, len(objects))
    if steps is not None:
        head += ", %s %d job step(s)" % (args.jobs, len(steps))
    print(head)
    for line in describe(report):
        print(line)

    if args.out:
        if args.apply:
            try:
                write_findings(args.out, report)
            except (IOError, OSError) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
    return gate(report)


if __name__ == "__main__":
    sys.exit(main())
