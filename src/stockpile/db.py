import boto3
import MySQLdb
import psycopg
from botocore.config import Config as BotoConfig

LOCAL_KEY = "stockpile"


def pg_connect(cfg, database=None, autocommit=True):
    return psycopg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password or None,
        dbname=database or cfg.database,
        autocommit=autocommit,
        options="-c timezone=UTC",
        connect_timeout=10,
    )


def mysql_connect(cfg, database=None, autocommit=True, local_infile=False):
    kwargs = {
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "password": cfg.password,
        "charset": "utf8mb4",
        "autocommit": autocommit,
        "connect_timeout": 10,
        "init_command": "SET time_zone = '+00:00'",
    }
    if database is not False:
        kwargs["database"] = database or cfg.database
    if local_infile:
        kwargs["local_infile"] = 1
    return MySQLdb.connect(**kwargs)


def dynamo_client(cfg, attempts=3, pool=64):
    options = {
        "region_name": cfg.region,
        "config": BotoConfig(
            max_pool_connections=pool,
            retries={"max_attempts": attempts, "mode": "standard"},
        ),
    }
    if cfg.mode != "aws":
        options["endpoint_url"] = cfg.endpoint
        options["aws_access_key_id"] = LOCAL_KEY
        options["aws_secret_access_key"] = LOCAL_KEY
    return boto3.session.Session().client("dynamodb", **options)


def pg_code(exc):
    return getattr(exc, "sqlstate", None)


def mysql_code(exc):
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None
