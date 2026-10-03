import os

import psycopg
from psycopg import sql


def main() -> None:
    with psycopg.connect(os.environ["CI_POSTGRES_ADMIN_URL"], autocommit=True) as db:
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier("rss_migrator"),
                sql.Literal(os.environ["CI_MIGRATION_PASSWORD"]),
            )
        )
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier("rss_agent_app"),
                sql.Literal(os.environ["CI_APP_PASSWORD"]),
            )
        )
        db.execute("GRANT CONNECT ON DATABASE rss_agent TO rss_migrator")
        db.execute("GRANT CONNECT ON DATABASE rss_agent TO rss_agent_app")
        db.execute("ALTER SCHEMA public OWNER TO rss_migrator")
        db.execute("GRANT USAGE ON SCHEMA public TO rss_agent_app")
        db.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE rss_migrator IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rss_agent_app"
        )


if __name__ == "__main__":
    main()
