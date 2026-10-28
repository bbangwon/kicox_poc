"""적재 CLI (docs/DB_LOADING_PLAN.md §7).

  uv run python -m ingest migrate
  uv run python -m ingest load [--src DIR] [--doc ID ...] [--dry-run]
  uv run python -m ingest sync [--doc ID ...]
  uv run python -m ingest check
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from ingest import config, db, gate, load, reader, sync
from ingest.embed import get_embedder

log = logging.getLogger("ingest")


def cmd_migrate(_args) -> int:
    applied = db.migrate()
    print(f"적용한 마이그레이션: {applied or '없음 (최신)'}")
    return 0


def cmd_load(args) -> int:
    src = Path(args.src)
    rejected: dict[str, list[str]] = {}
    results = []
    conn = None if args.dry_run else db.connect()
    for path in reader.doc_dirs(src, args.doc):
        doc_id = path.name
        try:
            a = reader.read(path)
        except reader.Rejected as e:
            rejected[doc_id] = e.reasons
            continue
        if reasons := gate.check(a):
            rejected[doc_id] = reasons
            continue
        if args.dry_run:
            results.append(load.Result(doc_id, "pass", load.counts(a)))
            continue
        results.append(r := load.load(conn, a))
        load.copy_files(path, doc_id)
        log.info("%s %s %s %s", doc_id, a.meta.name, r.action, r.counts or "")

    for doc_id, reasons in rejected.items():
        log.info("탈락 %s: %s", doc_id, " / ".join(reasons))
    by_reason = Counter(r.split(":")[0] for reasons in rejected.values() for r in reasons)
    by_action = Counter(r.action for r in results)
    print(f"\n대상 {len(results) + len(rejected)}건: {dict(by_action)} · 탈락 {len(rejected)}건")
    for reason, n in by_reason.most_common():
        print(f"  {reason}: {n}")
    if conn:
        conn.close()
    return 0


def cmd_sync(args) -> int:
    qc = sync.client()
    embedder = get_embedder()
    if embedder is None:
        log.warning("EMBED_BASE_URL이 없어 BM25만 색인합니다 (%s)", config.physical_name(config.CHUNKS))
    sync.ensure_collections(qc)
    with db.connect() as conn:
        try:
            avgdl = sync.configured_avgdl(conn)
        except sync.AvgdlNotSet as e:
            print(e)
            return 2
        docs = conn.execute(
            "SELECT id, regulation_key FROM documents WHERE status IN ('LOADED', 'INDEXED')"
            + (" AND id = ANY(%s)" if args.doc else "")
            + " ORDER BY id",
            (args.doc,) if args.doc else None,
        ).fetchall()
        failed = []
        for doc_id, _ in docs:
            try:
                s = sync.sync_doc(conn, qc, embedder, doc_id, avgdl)
                log.info("%s 포인트 %s · 임베딩 %d · 삭제 %d", doc_id, s.expected, s.embedded, s.deleted)
            except Exception:
                log.exception("%s 동기화 실패 (LOADED로 남음, 다음 실행에서 이어서 함)", doc_id)
                failed.append(doc_id)
        for key in sorted({k for _, k in docs}):
            if switched := sync.switch_current(conn, qc, key):
                current, superseded = switched
                log.info("현행 %s: %s%s", key, current, f" (이전 판 {superseded} → SUPERSEDED)" if superseded else "")
        problems = sync.check(conn, qc, avgdl)
    _report(problems)
    return 1 if failed or problems else 0


def cmd_check(_args) -> int:
    with db.connect() as conn:
        try:
            avgdl = sync.configured_avgdl(conn)
        except sync.AvgdlNotSet as e:
            print(e)
            return 2
        problems = sync.check(conn, sync.client(), avgdl)
    _report(problems)
    return 1 if problems else 0


def _report(problems: list[str]) -> None:
    print("점검: 정상" if not problems else "점검: 문제 " + str(len(problems)) + "건")
    for p in problems:
        print(f"  {p}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="DB 생성 · 마이그레이션 적용").set_defaults(func=cmd_migrate)
    lp = sub.add_parser("load", help="전처리 산출물 → PostgreSQL")
    lp.add_argument("--src", default=str(config.SRC_DIR))
    lp.add_argument("--doc", nargs="*")
    lp.add_argument("--dry-run", action="store_true", help="관문 판정만 하고 DB에 쓰지 않음")
    lp.set_defaults(func=cmd_load)
    sp = sub.add_parser("sync", help="PostgreSQL → Qdrant, 현행 판 전환, 점검")
    sp.add_argument("--doc", nargs="*")
    sp.set_defaults(func=cmd_sync)
    sub.add_parser("check", help="PostgreSQL ↔ Qdrant 점검").set_defaults(func=cmd_check)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
