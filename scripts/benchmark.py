"""Opt-in destructive workload on an EMPTY isolated benchmark database only."""

import argparse
import json
import statistics
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from openpyxl import Workbook
from sqlalchemy import func, select, text

from app.catalog import count_query, product_query
from app.config import settings
from app.models import Job, Product, SessionLocal, User, engine
from app.worker import process_one


def gtin(n):
    base = f"590{n:09d}"
    check = (-sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(base)))) % 10
    return base + str(check)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-isolated", action="store_true", required=True)
    parser.add_argument("--rows", type=int, default=50000)
    parser.add_argument("--output", default="benchmark.json")
    args = parser.parse_args()
    if engine.dialect.name != "postgresql" or "benchmark" not in (engine.url.database or ""):
        raise SystemExit("Use a separate PostgreSQL database with benchmark in its name; never production")
    command.upgrade(Config("alembic.ini"), "head")
    with SessionLocal() as s:
        if s.scalar(select(func.count()).select_from(Product)):
            raise SystemExit("Benchmark requires an empty product database")
        user = User(username="benchmark", password_hash="disabled", role="admin", active=False)
        s.add(user)
        s.commit()
        user_id = user.id
    file = settings.data_dir / "uploads" / "benchmark.xlsx"
    book = Workbook(write_only=True)
    sheet = book.create_sheet("Product List")
    categories = ["Yoghurt", "Cheese", "Fruit Juice", "Butter", "Pasta", "Chocolate", "Rice", "Milk"]
    brands = ["Mlekpol", "Lowicz", "Wawel", "Pudliszki", "Krasnystaw", "Wedel", "Lubella"]
    for i in range(args.rows):
        sheet.append(
            [
                f"M{i:07}",
                gtin(i),
                brands[i % len(brands)],
                "EACH",
                f"{150 + i % 10 * 50}G",
                f"{brands[i % len(brands)]} {categories[i % len(categories)]} Classic {i:05}",
            ]
        )
    book.save(file)

    def job_and_run(phase, old=None):
        with SessionLocal() as s:
            j = old or Job(
                kind="import",
                status="queued_preview",
                owner_id=user_id,
                payload={"source": file.name, "phase": phase},
            )
            if old:
                j = s.get(Job, old.id)
                j.payload = j.payload | {"phase": "commit"}
                j.status = "queued"
                j.total = args.rows
            s.add(j)
            s.commit()
        started = time.perf_counter()
        assert process_one()
        elapsed = time.perf_counter() - started
        with SessionLocal() as s:
            result = s.get(Job, j.id)
            assert result.status in ("preview", "completed"), (result.status, result.error, result.result)
            return elapsed, result

    preview_s, job = job_and_run("preview")
    import_s, job = job_and_run("commit", job)
    assert job.result["inserted"] == args.rows and job.result["failed"] == 0, job.result
    with engine.begin() as conn:
        conn.execute(text("ANALYZE products"))
        conn.execute(text("ANALYZE barcodes"))
    search = {}
    plans = {}
    for query in [gtin(args.rows // 2), "Yoghurt", "Lowicz", "not-present", "M0025000"]:
        timings = []
        for _ in range(30):
            with SessionLocal() as s:
                start = time.perf_counter()
                q = product_query(query)
                total = count_query(s, q)
                list(s.scalars(q.order_by(Product.name, Product.id).limit(30)))
                timings.append((time.perf_counter() - start) * 1000)
        search[query] = {
            "median_ms": round(statistics.median(timings), 2),
            "p95_ms": round(sorted(timings)[28], 2),
            "matches": total,
        }
        compiled = q.compile(engine, compile_kwargs={"literal_binds": True})
        with engine.connect() as conn:
            plans[query] = [r[0] for r in conn.execute(text("EXPLAIN ANALYZE " + str(compiled)))]
    # A repeat really runs the commit path, proving stable row count and identities.
    repeat_preview_s, repeat = job_and_run("preview")
    repeat_import_s, repeat = job_and_run("commit", repeat)
    with SessionLocal() as s:
        count = s.scalar(select(func.count()).select_from(Product))
    assert count == args.rows and repeat.result["inserted"] == 0 and repeat.result["failed"] == 0
    result = {
        "engine": "PostgreSQL",
        "server_version": engine.dialect.server_version_info,
        "rows": args.rows,
        "preview_seconds": round(preview_s, 3),
        "import_seconds": round(import_s, 3),
        "repeat_preview_seconds": round(repeat_preview_s, 3),
        "repeat_import_seconds": round(repeat_import_s, 3),
        "rows_after_repeat": count,
        "search_count_and_first_page": search,
        "query_plans": plans,
        "note": "Synthetic representative records. CI host performance is not the Ubuntu production server.",
    }
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
