import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .db import dynamo_client, mysql_connect, pg_connect

WINDOWS = os.name == "nt"


def binary(bin_dir, name):
    filename = f"{name}.exe" if WINDOWS else name
    if bin_dir and (Path(bin_dir) / filename).exists():
        return str(Path(bin_dir) / filename)
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"{name} not found in {bin_dir} or on PATH, see bin_dir in config.toml")
    return found


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_until(check, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {what}")


def spawn(args, log_path, cwd=None):
    log = open(log_path, "ab")
    options = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if cwd is not None:
        options["cwd"] = cwd
    if WINDOWS:
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(args, **options)
    finally:
        log.close()


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} failed: {result.stderr.strip() or result.stdout}")
    return result


def setting_line(name, value):
    if isinstance(value, bool):
        return f"{name} = {'on' if value else 'off'}"
    if isinstance(value, int | float):
        return f"{name} = {value}"
    return f"{name} = '{value}'"


class PostgresServer:
    name = "postgres"

    def __init__(self, cfg):
        self.cfg = cfg
        self.data = cfg.data_dir
        self.log = cfg.data_dir.parent / "postgres.log"

    def initialized(self):
        return (self.data / "PG_VERSION").exists()

    def init(self):
        if self.initialized():
            return False
        self.data.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                binary(self.cfg.bin_dir, "initdb"),
                "-D",
                str(self.data),
                "-U",
                self.cfg.user,
                "-A",
                "trust",
                "-E",
                "UTF8",
                "--no-locale",
            ]
        )
        with open(self.data / "postgresql.conf", "a", encoding="utf-8") as handle:
            handle.write("\ninclude_if_exists = 'stockpile.conf'\n")
        return True

    def write_settings(self):
        values = {
            "listen_addresses": self.cfg.host,
            "port": self.cfg.port,
            "unix_socket_directories": "",
            "timezone": "UTC",
            "log_timezone": "UTC",
            **self.cfg.settings,
        }
        text = "".join(setting_line(k, v) + "\n" for k, v in values.items())
        (self.data / "stockpile.conf").write_text(text, encoding="utf-8")

    def running(self):
        if not self.initialized():
            return False
        ctl = binary(self.cfg.bin_dir, "pg_ctl")
        return (
            subprocess.run([ctl, "-D", str(self.data), "status"], capture_output=True).returncode
            == 0
        )

    def start(self):
        self.init()
        if self.running():
            return False
        self.write_settings()
        ctl = binary(self.cfg.bin_dir, "pg_ctl")
        with open(self.data.parent / "pg_ctl.out", "ab") as out:
            started = subprocess.run(
                [ctl, "-D", str(self.data), "-l", str(self.log), "-w", "-t", "120", "start"],
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        if started.returncode != 0:
            raise RuntimeError(f"pg_ctl could not start postgres, see {self.log}")
        wait_until(self.ready, 60, "postgres")
        return True

    def stop(self):
        if not self.running():
            return False
        ctl = binary(self.cfg.bin_dir, "pg_ctl")
        run([ctl, "-D", str(self.data), "-m", "fast", "-w", "-t", "120", "stop"])
        return True

    def ready(self):
        try:
            with pg_connect(self.cfg, database="postgres") as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False


class MySQLServer:
    name = "mysql"

    def __init__(self, cfg):
        self.cfg = cfg
        self.data = cfg.data_dir
        self.base = cfg.data_dir.parent
        self.cnf = self.base / "mysql.cnf"
        self.log = self.base / "mysql.err"

    def initialized(self):
        return (self.data / "mysql").exists()

    def write_cnf(self):
        files = self.base / "mysql-files"
        files.mkdir(parents=True, exist_ok=True)
        lines = [
            "[mysqld]",
            f"datadir = {self.data.resolve().as_posix()}",
            f"port = {self.cfg.port}",
            f"bind-address = {self.cfg.host}",
            "mysqlx = OFF",
            "local-infile = ON",
            f"log-error = {self.log.resolve().as_posix()}",
            f"pid-file = {(self.base / 'mysql.pid').resolve().as_posix()}",
            f"secure-file-priv = {files.resolve().as_posix()}",
            "character-set-server = utf8mb4",
            "collation-server = utf8mb4_0900_bin",
        ]
        if not WINDOWS:
            lines.append(f"socket = {(self.base / 'mysql.sock').resolve().as_posix()}")
        if not self.cfg.binlog:
            lines.append("skip-log-bin")
        for name, value in self.cfg.settings.items():
            if isinstance(value, bool):
                value = "ON" if value else "OFF"
            lines.append(f"{name} = {value}")
        self.cnf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def mysqld(self):
        return binary(self.cfg.bin_dir, "mysqld")

    def init(self):
        if self.initialized():
            return False
        self.base.mkdir(parents=True, exist_ok=True)
        self.write_cnf()
        run([self.mysqld(), f"--defaults-file={self.cnf.resolve()}", "--initialize-insecure"])
        return True

    def running(self):
        return port_open(self.cfg.host, self.cfg.port)

    def start(self):
        self.init()
        if self.running():
            return False
        self.write_cnf()
        proc = spawn(
            [self.mysqld(), f"--defaults-file={self.cnf.resolve()}"], self.base / "mysqld.out"
        )
        wait_until(lambda: self.ready() or proc.poll() is not None, 180, "mysqld")
        if proc.poll() is not None:
            raise RuntimeError(f"mysqld exited, see {self.log}")
        return True

    def ready(self):
        try:
            conn = mysql_connect(self.cfg, database=False)
            conn.close()
            return True
        except Exception:
            return False

    def stop(self):
        if not self.running():
            return False
        conn = mysql_connect(self.cfg, database=False)
        try:
            conn.cursor().execute("SHUTDOWN")
        finally:
            conn.close()
        wait_until(lambda: not self.running(), 120, "mysqld to stop")
        return True


class DynamoServer:
    name = "dynamodb"

    def __init__(self, cfg):
        self.cfg = cfg
        self.data = cfg.data_dir
        self.pid_file = cfg.data_dir / "dynamodb.pid"
        url = urlparse(cfg.endpoint)
        self.host = url.hostname or "127.0.0.1"
        self.port = url.port or 8000

    def running(self):
        return port_open(self.host, self.port)

    def start(self):
        if self.running():
            return False
        if self.cfg.local_dir is None:
            raise RuntimeError("set dynamodb.local_dir to the folder with DynamoDBLocal.jar")
        folder = self.cfg.local_dir
        jar = folder / "DynamoDBLocal.jar"
        if not jar.exists():
            raise RuntimeError(f"{jar} not found")
        self.data.mkdir(parents=True, exist_ok=True)
        args = [
            self.cfg.java,
            f"-Xmx{self.cfg.heap}",
            f"-Djava.library.path={folder / 'DynamoDBLocal_lib'}",
            "-jar",
            str(jar),
            "-sharedDb",
            *(["-inMemory"] if self.cfg.in_memory else ["-dbPath", str(self.data)]),
            "-port",
            str(self.port),
        ]
        proc = spawn(args, self.data.parent / "dynamodb.log", cwd=self.data)
        self.pid_file.write_text(str(proc.pid), encoding="utf-8")
        wait_until(lambda: self.ready() or proc.poll() is not None, 120, "DynamoDB Local")
        if proc.poll() is not None:
            raise RuntimeError(f"DynamoDB Local exited, see {self.data.parent / 'dynamodb.log'}")
        return True

    def ready(self):
        if not self.running():
            return False
        try:
            dynamo_client(self.cfg, attempts=1).list_tables(Limit=1)
            return True
        except Exception:
            return False

    def stop(self):
        if not self.running() or not self.pid_file.exists():
            return False
        pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        wait_until(lambda: not self.running(), 60, "DynamoDB Local to stop")
        self.pid_file.unlink(missing_ok=True)
        return True


def server_for(cfg, engine):
    if engine == "postgres":
        return PostgresServer(cfg.postgres)
    if engine == "mysql":
        return MySQLServer(cfg.mysql)
    return DynamoServer(cfg.dynamodb)


def managed(cfg, engine):
    return getattr(cfg, engine).mode == "managed"


def reachable(cfg, engine):
    server = server_for(cfg, engine)
    if engine == "dynamodb" and cfg.dynamodb.mode == "aws":
        try:
            dynamo_client(cfg.dynamodb, attempts=1).list_tables(Limit=1)
            return True
        except Exception:
            return False
    return server.ready()
