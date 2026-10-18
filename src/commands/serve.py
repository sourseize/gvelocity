"""
velocity serve — run Velocity 24×7: poll for new holdings, run the strategy's sessions, serve the
web pages.

  velocity serve                                   # poll every 10 min, http://127.0.0.1:8787/
  velocity serve --poll-frequency 5 --port 9000
  velocity serve --param breakout_len=40 --max-positions 30 --accept-param-change

Change any setting by restarting.  The settings must match the ones the book's last session ran
with — otherwise `serve` refuses, unless --accept-param-change is given (the change is then
recorded on every following session).

While it runs it holds the instance lock: the CLI commands that need the database are refused
until it is stopped (Ctrl-C / SIGTERM; the current cycle finishes first).
"""
import logging
import signal
import threading

from ..scheduler import Scheduler, record_start, record_stop
from ..web.server import WebServer
from ..exceptions import VelocityError
from ..strategy import make_strategy
from ._common import (add_param_arg, add_engine_args, engine_config, check_strategy_params, check_db, open_db,
                      require_history)

log = logging.getLogger(__name__)
HELP = "Run 24x7: poll for new holdings every N minutes, run the strategy's sessions, serve the web pages"


def configure(p) -> None:
    add_param_arg(p)
    add_engine_args(p)
    p.add_argument("--poll-frequency", type=float, default=10.0, metavar="MINUTES",
                   help="Minutes between holdings polls, round the clock (default: 10)")
    p.add_argument("--no-catchup", action="store_true",
                   help="After downtime run only the latest session instead of every missed one in order")
    p.add_argument("--accept-param-change", action="store_true",
                   help="Continue the book even though the settings differ from its last session")
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit (testing / cron fallback)")
    p.add_argument("--host", default="127.0.0.1", help="Address the web service binds to (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787, help="Port of the web service (default: 8787)")
    p.add_argument("--no-web", action="store_true", help="Poll and trade only, no web service")


def validate(p, args) -> None:
    if args.poll_frequency <= 0:
        p.error("--poll-frequency must be > 0 minutes")
    if not 0 < args.port < 65536:
        p.error("--port must be between 1 and 65535")
    args.params = check_strategy_params(p, args)
    check_db(p)


def run(args) -> int:
    conn = open_db(must_exist=True)
    try:
        require_history(conn)
        strategy = make_strategy(**args.params)
        cfg = engine_config(args)
        sched = Scheduler(conn, args.params, cfg, args.capital,
                          poll_frequency=args.poll_frequency, catchup=not args.no_catchup)
        sched.check_settings(accept_change=args.accept_param_change)      # raises ParamsChanged -> exit 1
        record_start(conn, strategy, cfg, args.capital, args.poll_frequency)
        log.info("serve: %s (%s) · poll every %g min · %s", strategy.label, strategy.name, args.poll_frequency,
                 f"{cfg.max_positions} positions ({cfg.policy.mode}) x {(1 - cfg.cash_reserve) / cfg.max_positions:.1%} of equity, "
                 f"cash reserve {cfg.cash_reserve:.0%}")
        if args.once:
            sched.cycle()
            return 0

        web = None
        if not args.no_web:
            try:
                web = WebServer(sched, args.host, args.port)
            except OSError as e:
                raise VelocityError(f"cannot bind the web service to {args.host}:{args.port}: {e.strerror or e}") from e
            web.start()
        stop = lambda signum, frame: (log.info("signal %d — stopping after the current cycle", signum), sched.stop())
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        worker = threading.Thread(target=sched.run_forever, name="poller", daemon=True)
        worker.start()
        while worker.is_alive():          # the main thread only waits for the stop signal
            worker.join(timeout=1.0)
        if web is not None:
            web.stop()
        return 0
    finally:
        try:
            record_stop(conn)
        finally:
            conn.close()
