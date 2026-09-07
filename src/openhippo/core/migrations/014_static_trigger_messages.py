"""Replace dynamic RAISE messages for pre-3.47 SQLite portability.

The original migrations are also corrected for fresh databases. Existing
version-13 databases only run this DDL; memory/audit rows are never touched.
Execute individual statements so the migration runner can roll back all DDL.
"""

def upgrade(conn):
    statements = [
        ("cold_memory_bi_dreamstatus", """
            CREATE TRIGGER cold_memory_bi_dreamstatus
            BEFORE INSERT ON cold_memory
            WHEN NEW.dream_status NOT IN ('active', 'dormant', 'consolidated')
            BEGIN SELECT RAISE(ABORT, 'invalid dream_status'); END
        """),
        ("cold_memory_bu_dreamstatus", """
            CREATE TRIGGER cold_memory_bu_dreamstatus
            BEFORE UPDATE OF dream_status ON cold_memory
            WHEN NEW.dream_status NOT IN ('active', 'dormant', 'consolidated')
            BEGIN SELECT RAISE(ABORT, 'invalid dream_status'); END
        """),
        ("dream_actions_bi_action", """
            CREATE TRIGGER dream_actions_bi_action
            BEFORE INSERT ON dream_actions
            WHEN NEW.action NOT IN (
                'consolidate_member', 'consolidate_seed', 'forget', 'restore',
                'mark_dormant', 'restore_dormant', 'purge_dormant', 'consolidate_promoted'
            )
            BEGIN SELECT RAISE(ABORT, 'invalid dream_action'); END
        """),
    ]
    for name, sql in statements:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        conn.execute(sql)
