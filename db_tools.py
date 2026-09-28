import os
import re
from contextlib import contextmanager
from functools import wraps
from typing import Any, Literal

import mysql.connector
from langchain.tools import tool
from pydantic import BaseModel

MAX_ROWS = 200

# Columns the agent may never read or write.
HIDDEN_COLUMNS = {"users": {"password"}}

# Columns managed by the database; the agent may not set them.
READ_ONLY_COLUMNS = {"id", "created_at"}

# The schema declares no foreign keys, so the logical relationships are listed
# here: (child_table, column) -> parent_table (always the parent's `id`).
REFERENCES = {
    ("appraisals", "scout_id"): "personnel",
    ("appraisals", "subject_id"): "personnel",
    ("appraisal_attributes", "appraisal_id"): "appraisals",
    ("appraisal_attributes", "attribute_id"): "attributes",
    ("personnel_drills", "personnel_id"): "personnel",
    ("personnel_drills", "drill_id"): "drills",
    ("personnel_events", "personnel_id"): "personnel",
    ("personnel_events", "event_id"): "events",
}


class Filter(BaseModel):
    column: str
    op: Literal["=", "!=", "<", "<=", ">", ">=", "LIKE", "IS NULL", "IS NOT NULL"] = "="
    value: str | int | float | None = None


@contextmanager
def _connect():
    connection = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
    )
    try:
        yield connection
    finally:
        connection.close()


def _safe(fn):
    """Return database/validation errors to the agent instead of raising."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (mysql.connector.Error, ValueError) as e:
            return {"error": str(e)}
    return wrapper


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _rows(rows):
    return [{k: _jsonable(v) for k, v in row.items()} for row in rows]


def _table_columns(cursor, table: str) -> list[dict]:
    """Validate `table` against the live schema and return its visible columns."""
    cursor.execute("SHOW TABLES")
    tables = [next(iter(row.values())) for row in cursor.fetchall()]
    if table not in tables:
        raise ValueError(f"Unknown table '{table}'. Available tables: {', '.join(tables)}")
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    hidden = HIDDEN_COLUMNS.get(table, set())
    return [c for c in cursor.fetchall() if c["Field"] not in hidden]


def _check_columns(table: str, names, valid: list[str]):
    bad = [n for n in names if n not in valid]
    if bad:
        raise ValueError(f"Unknown column(s) {bad} for table '{table}'. Columns: {valid}")


def _where(table: str, filters: list[Filter] | None, valid: list[str]):
    if not filters:
        return "", []
    _check_columns(table, [f.column for f in filters], valid)
    clauses, params = [], []
    for f in filters:
        if f.op in ("IS NULL", "IS NOT NULL"):
            clauses.append(f"`{f.column}` {f.op}")
        else:
            clauses.append(f"`{f.column}` {f.op} %s")
            params.append(f.value)
    return " WHERE " + " AND ".join(clauses), params


def _check_references(cursor, table: str, values: dict):
    for column, value in values.items():
        parent = REFERENCES.get((table, column))
        if parent is None or value is None:
            continue
        cursor.execute(f"SELECT 1 FROM `{parent}` WHERE id = %s", (value,))
        if cursor.fetchone() is None:
            raise ValueError(f"{table}.{column} = {value} does not match any '{parent}' record")


def _writable_values(table: str, values: dict, columns: list[dict]) -> dict:
    if not values:
        raise ValueError("No values provided")
    valid = [c["Field"] for c in columns if c["Field"] not in READ_ONLY_COLUMNS]
    _check_columns(table, values.keys(), valid)
    return values


@tool("describeDatabase", description="List every table with its columns (name, type, nullable, key) and the logical relationships between tables. Call this first if unsure of the schema.", return_direct=False)
@_safe
def describeDatabase():
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SHOW TABLES")
        tables = [next(iter(row.values())) for row in cursor.fetchall()]
        schema = {}
        for table in tables:
            schema[table] = [
                {"name": c["Field"], "type": c["Type"], "nullable": c["Null"] == "YES", "key": c["Key"]}
                for c in _table_columns(cursor, table)
            ]
    relationships = [f"{child}.{col} -> {parent}.id" for (child, col), parent in REFERENCES.items()]
    return {"tables": schema, "logical_relationships": relationships}


@tool("queryRecords", description=f"Read rows from one table. Filters are ANDed together; each has a column, an op (=, !=, <, <=, >, >=, LIKE, IS NULL, IS NOT NULL) and a value. Returns at most {MAX_ROWS} rows; use offset to page.", return_direct=False)
@_safe
def queryRecords(
    table: str,
    filters: list[Filter] | None = None,
    columns: list[str] | None = None,
    order_by: str | None = None,
    descending: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        valid = [c["Field"] for c in _table_columns(cursor, table)]
        selected = columns or valid
        _check_columns(table, selected, valid)
        where, params = _where(table, filters, valid)
        sql = f"SELECT {', '.join(f'`{c}`' for c in selected)} FROM `{table}`{where}"
        if order_by:
            _check_columns(table, [order_by], valid)
            sql += f" ORDER BY `{order_by}` {'DESC' if descending else 'ASC'}"
        sql += " LIMIT %s OFFSET %s"
        cursor.execute(sql, params + [max(1, min(limit, MAX_ROWS)), max(0, offset)])
        return _rows(cursor.fetchall())


@tool("countRecords", description="Count rows in a table, optionally matching filters (same filter format as queryRecords).", return_direct=False)
@_safe
def countRecords(table: str, filters: list[Filter] | None = None):
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        valid = [c["Field"] for c in _table_columns(cursor, table)]
        where, params = _where(table, filters, valid)
        cursor.execute(f"SELECT COUNT(*) AS count FROM `{table}`{where}", params)
        return cursor.fetchone()


@tool("runSelectQuery", description=f"Run a single read-only SELECT (or WITH ... SELECT) statement for joins, aggregates and rankings that queryRecords can't express. Returns at most {MAX_ROWS} rows. The users table is not accessible.", return_direct=False)
@_safe
def runSelectQuery(sql: str):
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise ValueError("Only a single statement is allowed")
    if not re.match(r"(select|with)\b", statement, re.I):
        raise ValueError("Only SELECT queries are allowed")
    if re.search(r"\b(users|password)\b|into\s+(outfile|dumpfile)", statement, re.I):
        raise ValueError("That table, column or clause is not accessible")
    with _connect() as conn:
        conn.start_transaction(readonly=True)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(statement)
        rows = cursor.fetchmany(MAX_ROWS + 1)
    return {"rows": _rows(rows[:MAX_ROWS]), "truncated": len(rows) > MAX_ROWS}


@tool("createRecord", description="Insert a row into a table. `values` maps column names to values; id and created_at are set automatically. Returns the new row's id.", return_direct=False)
@_safe
def createRecord(table: str, values: dict[str, Any]):
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        columns = _table_columns(cursor, table)
        values = _writable_values(table, values, columns)
        _check_references(cursor, table, values)
        names = ", ".join(f"`{c}`" for c in values)
        placeholders = ", ".join(["%s"] * len(values))
        cursor.execute(f"INSERT INTO `{table}` ({names}) VALUES ({placeholders})", list(values.values()))
        conn.commit()
        return {"created_id": cursor.lastrowid}


@tool("updateRecord", description="Update columns of one row, identified by its id. `values` maps column names to new values.", return_direct=False)
@_safe
def updateRecord(table: str, record_id: int, values: dict[str, Any]):
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        columns = _table_columns(cursor, table)
        values = _writable_values(table, values, columns)
        _check_references(cursor, table, values)
        assignments = ", ".join(f"`{c}` = %s" for c in values)
        cursor.execute(f"UPDATE `{table}` SET {assignments} WHERE id = %s", list(values.values()) + [record_id])
        conn.commit()
        if cursor.rowcount == 0:
            cursor.execute(f"SELECT 1 FROM `{table}` WHERE id = %s", (record_id,))
            if cursor.fetchone() is None:
                raise ValueError(f"No '{table}' record with id {record_id}")
        return {"updated_id": record_id}


@tool("deleteRecord", description="Delete one row by id. Refused if other records still reference it; delete those first.", return_direct=False)
@_safe
def deleteRecord(table: str, record_id: int):
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        _table_columns(cursor, table)
        for (child, column), parent in REFERENCES.items():
            if parent != table:
                continue
            cursor.execute(f"SELECT COUNT(*) AS count FROM `{child}` WHERE `{column}` = %s", (record_id,))
            count = cursor.fetchone()["count"]
            if count:
                raise ValueError(f"Cannot delete: {count} '{child}' record(s) reference this via {column}. Delete those first.")
        cursor.execute(f"DELETE FROM `{table}` WHERE id = %s", (record_id,))
        conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"No '{table}' record with id {record_id}")
        return {"deleted_id": record_id}


@tool("getPersonnelProfile", description="Get everything known about one person: their record, drill results, event participation, and appraisals received (with attribute scores and scout).", return_direct=False)
@_safe
def getPersonnelProfile(personnel_id: int):
    with _connect() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM personnel WHERE id = %s", (personnel_id,))
        person = cursor.fetchone()
        if person is None:
            raise ValueError(f"No personnel record with id {personnel_id}")

        cursor.execute(
            "SELECT d.name AS drill, pd.score, pd.date FROM personnel_drills pd "
            "JOIN drills d ON d.id = pd.drill_id WHERE pd.personnel_id = %s ORDER BY pd.date",
            (personnel_id,),
        )
        drills = cursor.fetchall()

        cursor.execute(
            "SELECT e.name AS event, pe.date FROM personnel_events pe "
            "JOIN events e ON e.id = pe.event_id WHERE pe.personnel_id = %s ORDER BY pe.date",
            (personnel_id,),
        )
        events = cursor.fetchall()

        cursor.execute(
            "SELECT a.id AS appraisal_id, a.created_at, a.scout_id, "
            "CONCAT(s.first_name, ' ', s.last_name) AS scout, attr.name AS attribute, aa.score "
            "FROM appraisals a "
            "LEFT JOIN personnel s ON s.id = a.scout_id "
            "LEFT JOIN appraisal_attributes aa ON aa.appraisal_id = a.id "
            "LEFT JOIN attributes attr ON attr.id = aa.attribute_id "
            "WHERE a.subject_id = %s ORDER BY a.created_at, a.id",
            (personnel_id,),
        )
        appraisals: dict[int, dict] = {}
        for row in cursor.fetchall():
            entry = appraisals.setdefault(row["appraisal_id"], {
                "appraisal_id": row["appraisal_id"],
                "created_at": _jsonable(row["created_at"]),
                "scout_id": row["scout_id"],
                "scout": row["scout"],
                "scores": [],
            })
            if row["attribute"] is not None:
                entry["scores"].append({"attribute": row["attribute"], "score": row["score"]})

    return {
        "personnel": _rows([person])[0],
        "drills": _rows(drills),
        "events": _rows(events),
        "appraisals": list(appraisals.values()),
    }


DATABASE_TOOLS = [
    describeDatabase,
    queryRecords,
    countRecords,
    runSelectQuery,
    getPersonnelProfile,
    createRecord,
    updateRecord,
    deleteRecord,
]
