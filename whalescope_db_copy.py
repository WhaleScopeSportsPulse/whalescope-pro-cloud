"""One-off copy of the expiring Render database (whalescope-db) into Neon.

Run as a Render cron job in the same region as the source so it can use the internal URL:
  SOURCE_DATABASE_URL  whalescope-db internal URL (pasted in the Render dashboard; never logged)
  TARGET_DATABASE_URL  Neon `whalescope` database

The source is opened read-only. Nothing is ever deleted or overwritten: a target table that
already has rows is only re-verified. Safe to run repeatedly; once every table verifies, later
runs just print the stored result.
"""
import os
import sys
import time

import psycopg

SYSTEM = ("pg_catalog", "information_schema", "pg_toast")
MAX_SOURCE_BYTES = int(os.getenv("MAX_SOURCE_BYTES", str(700 * 1024 * 1024)))
CHECKSUM_MAX_ROWS = int(os.getenv("CHECKSUM_MAX_ROWS", "500000"))
LOG = "_whalescope_db_copy_log"


def q(name):
    return '"' + name.replace('"', '""') + '"'


def tables(src):
    return src.execute("""
        SELECT n.nspname, c.relname, c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p') AND n.nspname NOT IN %s AND n.nspname NOT LIKE 'pg_temp%%'
        ORDER BY 1, 2""" % (SYSTEM,)).fetchall()


def columns(src, oid):
    return src.execute("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attidentity
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,)).fetchall()


def constraints(src, oid, kinds):
    return src.execute("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = %s AND contype = ANY(%s) ORDER BY conname""", (oid, list(kinds))).fetchall()


def plain_indexes(src, oid):
    return [r[0] for r in src.execute("""
        SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i
        WHERE i.indrelid = %s AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
        ORDER BY 1""", (oid,)).fetchall()]


def count(conn, fq):
    return conn.execute("SELECT count(*) FROM " + fq).fetchone()[0]


def checksum(conn, fq):
    return conn.execute("SELECT md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM " + fq + " t").fetchone()[0]


def main():
    src_url, dst_url = os.getenv("SOURCE_DATABASE_URL", ""), os.getenv("TARGET_DATABASE_URL", "")
    if not src_url or not dst_url:
        print("COPY WAITING: set", " and ".join(k for k, v in (("SOURCE_DATABASE_URL", src_url), ("TARGET_DATABASE_URL", dst_url)) if not v), flush=True)
        return 0
    # Identical text output on both sides, so row checksums compare like for like.
    same_text = "SET TIME ZONE 'UTC'; SET DateStyle = 'ISO, MDY'; SET IntervalStyle = 'postgres'; SET extra_float_digits = 1"
    dst = psycopg.connect(dst_url, autocommit=True)
    dst.execute(same_text)
    dst.execute(f"CREATE TABLE IF NOT EXISTS {LOG}(schema_name TEXT, table_name TEXT, source_rows BIGINT, target_rows BIGINT,"
                " source_md5 TEXT, target_md5 TEXT, status TEXT, checked_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(schema_name, table_name))")
    dst.execute(f"CREATE TABLE IF NOT EXISTS {LOG}_done(finished_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    finished = dst.execute(f"SELECT max(finished_at) FROM {LOG}_done").fetchone()[0]
    if finished:
        # Never touch the source again once a copy verified; it may no longer exist.
        print("COPY ALREADY COMPLETE at", finished.isoformat(), "- nothing to do", flush=True)
        for r in dst.execute(f"SELECT schema_name, table_name, source_rows, target_rows, status FROM {LOG} ORDER BY 1, 2"):
            print("COPY VERIFIED", *r, flush=True)
        return 0
    done = dst.execute(f"SELECT count(*), count(*) FILTER (WHERE status='verified') FROM {LOG}").fetchone()
    src = psycopg.connect(src_url, autocommit=False)
    src.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    src.execute(same_text)
    size = src.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    found = tables(src)
    print("COPY SOURCE", len(found), "tables,", round(size / 1048576, 1), "MB", flush=True)
    if done[0] and done[0] == done[1] == len(found):
        for r in dst.execute(f"SELECT schema_name, table_name, source_rows, target_rows, status FROM {LOG} ORDER BY 1, 2"):
            print("COPY ALREADY VERIFIED", *r, flush=True)
        dst.execute(f"INSERT INTO {LOG}_done DEFAULT VALUES")
        return 0
    if size > MAX_SOURCE_BYTES:
        print("COPY ABORTED: source is larger than MAX_SOURCE_BYTES; nothing copied", flush=True)
        return 1

    sequences = []
    fks = []
    ok = True
    for schema, name, oid in found:
        fq = q(schema) + "." + q(name)
        dst.execute("CREATE SCHEMA IF NOT EXISTS " + q(schema))
        exists = dst.execute("SELECT to_regclass(%s)", (fq,)).fetchone()[0]
        cols = columns(src, oid)
        if not exists:
            defs = []
            for col, typ, notnull, default, identity in cols:
                d = q(col) + " " + typ
                if identity:
                    d += " GENERATED " + ("ALWAYS" if identity == "a" else "BY DEFAULT") + " AS IDENTITY"
                elif default:
                    if default.startswith("nextval("):
                        seq = default.split("'")[1]
                        dst.execute("CREATE SEQUENCE IF NOT EXISTS " + seq)
                        sequences.append((seq, fq, col))
                    d += " DEFAULT " + default
                if notnull:
                    d += " NOT NULL"
                defs.append(d)
            dst.execute("CREATE TABLE " + fq + " (" + ", ".join(defs) + ")")
        target_rows = count(dst, fq)
        names = ", ".join(q(c[0]) for c in cols)
        if target_rows == 0:
            t0 = time.time()
            with src.cursor().copy("COPY " + fq + " (" + names + ") TO STDOUT") as out, \
                    dst.cursor().copy("COPY " + fq + " (" + names + ") FROM STDIN") as inp:
                for chunk in out:
                    inp.write(chunk)
            print("COPY ROWS", fq, "in", round(time.time() - t0, 1), "s", flush=True)
        else:
            print("COPY TARGET NOT EMPTY", fq, target_rows, "rows; verifying only", flush=True)
        if not exists:
            for con, definition in constraints(src, oid, "puc"):
                dst.execute("ALTER TABLE " + fq + " ADD CONSTRAINT " + q(con) + " " + definition)
            for idx in plain_indexes(src, oid):
                dst.execute(idx)
            fks += [(fq, con, definition) for con, definition in constraints(src, oid, "f")]
        s_rows, d_rows = count(src, fq), count(dst, fq)
        s_md5 = d_md5 = None
        if s_rows <= CHECKSUM_MAX_ROWS:
            s_md5, d_md5 = checksum(src, fq), checksum(dst, fq)
        status = "verified" if s_rows == d_rows and s_md5 == d_md5 else "MISMATCH"
        ok &= status == "verified"
        dst.execute(f"INSERT INTO {LOG} VALUES (%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (schema_name, table_name) DO UPDATE SET"
                    " source_rows=EXCLUDED.source_rows, target_rows=EXCLUDED.target_rows, source_md5=EXCLUDED.source_md5,"
                    " target_md5=EXCLUDED.target_md5, status=EXCLUDED.status, checked_at=now()",
                    (schema, name, s_rows, d_rows, s_md5, d_md5, status))
        print("COPY CHECK", fq, "source", s_rows, "target", d_rows, "checksum", "match" if s_md5 == d_md5 else "DIFFERS", status, flush=True)
    for fq, con, definition in fks:
        dst.execute("ALTER TABLE " + fq + " ADD CONSTRAINT " + q(con) + " " + definition)
    for seq, fq, col in sequences:
        dst.execute(f"SELECT setval(%s, coalesce((SELECT max({q(col)}) FROM {fq}), 0) + 1, false)", (seq,))
    src.rollback()
    if ok:
        dst.execute(f"INSERT INTO {LOG}_done DEFAULT VALUES")
    print("COPY DONE" if ok else "COPY FINISHED WITH MISMATCHES", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
